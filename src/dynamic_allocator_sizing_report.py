"""Research-only dynamic allocator sizing diagnostics."""

from __future__ import annotations

import ast
import gzip
import json
import math
import re
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Any

_ISO_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
_COMPACT_DATE_RE = re.compile(r"(\d{8})")
_KV_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=([^ ]+)")
_ENTRY_EVAL_RE = re.compile(
    r"\b(?P<symbol>[A-Z][A-Z0-9.\-]{0,9})\s+ENTRY_EVAL\s+"
    r"route=(?P<route>[^ ]+).*?\bfinal=(?P<final>[TF]|true|false|True|False)\b.*?"
    r"\breason=(?P<reason>.+?)(?=\s+[A-Za-z_][A-Za-z0-9_]*=|$)"
)
_SKIP_RE = re.compile(r"\bSKIP\s+(?P<symbol>[A-Z][A-Z0-9.\-]{0,9}):\s+(?P<body>.*)$")
_FOCUS_SYMBOLS = ("INTC", "AMD", "AKTS", "FRD", "ZDGE", "NOK", "IWM", "XLF")
_MIN_DEPLOY_BUFFER = 5.0


@dataclass(frozen=True)
class DynamicAllocatorSizingPaths:
    json_path: Path
    text_path: Path


def _safe_user(user_id: str) -> str:
    return "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in str(user_id or "default")) or "default"


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip().strip("$,;")
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


def _date_from_compact(raw: str) -> str | None:
    if len(raw) == 8 and raw.isdigit():
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    return None


def _date_from_path(path: Path) -> str | None:
    for part in [path.name, *[parent.name for parent in path.parents]]:
        iso = _ISO_DATE_RE.search(part)
        if iso:
            return iso.group(1)
        compact = _COMPACT_DATE_RE.search(part)
        if compact:
            return _date_from_compact(compact.group(1))
    return None


def _read_text(path: Path) -> str:
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    return path.read_text(encoding="utf-8", errors="replace")


def _parse_kv(line: str) -> dict[str, str]:
    return {match.group(1): match.group(2).strip(",;") for match in _KV_RE.finditer(line)}


def _parse_symbol_list(line: str, marker: str) -> list[str]:
    text = line.split(marker, 1)[1] if marker in line else line
    match = re.search(r"\[(?P<body>[^\]]*)\]", text)
    if not match:
        return []
    payload = "[" + match.group("body") + "]"
    try:
        parsed = ast.literal_eval(payload)
    except Exception:
        parsed = None
    if isinstance(parsed, list):
        return [str(item).strip().upper() for item in parsed if str(item).strip()]
    return [symbol.upper() for symbol in re.findall(r"[A-Z][A-Z0-9.\-]{0,9}", payload)]


def _extract_trade_size(text: str) -> float | None:
    match = re.search(r"trade_size\s+\$?([0-9]+(?:\.[0-9]+)?)", text)
    return _round(match.group(1), 2) if match else None


def _extract_minimum_cash(text: str) -> float | None:
    match = re.search(r"minimum_cash_to_deploy\s+([0-9]+(?:\.[0-9]+)?)", text)
    return _round(match.group(1), 2) if match else None


def _blank_row(symbol: str) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "route": None,
        "source": None,
        "dynamic_candidate": False,
        "entry_eval_final": None,
        "entry_eval_reason": None,
        "reached_allocator": False,
        "ranked": False,
        "selected": False,
        "allocator_size_trace": None,
        "candidate_rank": None,
        "score": None,
        "strength": None,
        "account_equity": None,
        "raw_desired_notional": None,
        "raw_target_notional": None,
        "target_pct": None,
        "final_trade_size": None,
        "minimum_cash_to_deploy": None,
        "available_cash": None,
        "gross_headroom": None,
        "position_cap": None,
        "sector_cap": None,
        "symbol_cap": None,
        "per_trade_cap": None,
        "sleeve_allocation_cap": None,
        "existing_position": None,
        "candidate_notional_requested": None,
        "candidate_requested_notional": None,
        "candidate_notional": None,
        "candidate_notional_cap": None,
        "base_requested_notional": None,
        "tranche_min": None,
        "target_allocation": None,
        "current_dynamic_sleeve_usage": None,
        "dynamic_sleeve_cap": None,
        "core_sleeve_cap": None,
        "sector_cap_remaining": None,
        "symbol_cap_remaining": None,
        "position_cap_remaining": None,
        "max_trade_size": None,
        "after_sleeve_cap": None,
        "after_sector_cap": None,
        "after_symbol_cap": None,
        "after_position_cap": None,
        "after_gross_headroom": None,
        "min_order_notional": None,
        "max_single_dynamic_notional": None,
        "limiting_cap": None,
        "clipping_steps_detected": [],
        "first_clipping_stage": None,
        "first_below_min_deploy_stage": None,
        "sizing_formula_inference": "not_observed",
        "final_skip_reason": None,
        "detail": None,
        "evidence": [],
    }


def _row(rows: dict[str, dict[str, Any]], symbol: str) -> dict[str, Any]:
    sym = str(symbol or "").strip().upper()
    if sym not in rows:
        rows[sym] = _blank_row(sym)
    return rows[sym]


def _note(row: dict[str, Any], path: Path, line_no: int, line: str) -> None:
    if len(row["evidence"]) < 12:
        row["evidence"].append({"source_file": str(path), "line_number": line_no, "line": line.strip()})


def _mark_dynamic(row: dict[str, Any]) -> None:
    route = str(row.get("route") or "").lower()
    source = str(row.get("source") or "").lower()
    if "dynamic" in route or "dynamic" in source or row.get("dynamic_candidate") is True:
        row["dynamic_candidate"] = True


def _discover_log_paths(project_root: Path, *, data_dir: Path, day: str, extra_paths: Sequence[Path | str] | None) -> list[Path]:
    paths: list[Path] = []
    review_dir = data_dir / "review" / day
    if review_dir.exists():
        for pattern in ("paper_full.log", "*.log", "*.txt", "*.log.gz"):
            paths.extend(path for path in review_dir.glob(pattern) if path.is_file())
    for base in (data_dir / "logs", data_dir / "debug_logs", project_root / "reports" / "debug"):
        if not base.exists():
            continue
        iterator = base.rglob("*") if base.name == "debug_logs" else base.glob("*")
        for path in iterator:
            if not path.is_file() or path.suffix not in {".log", ".txt", ".gz"}:
                continue
            path_day = _date_from_path(path)
            if path_day == day or day in str(path):
                paths.append(path)
    for raw in extra_paths or []:
        path = Path(raw)
        if path.exists() and path.is_file():
            paths.append(path)
    return sorted(dict.fromkeys(paths))


def latest_dynamic_allocator_sizing_date(
    *,
    project_root: Path | str = ".",
    data_dir: Path | str = "data",
    user_id: str = "paper_bot",
) -> str | None:
    del project_root, user_id
    data = Path(data_dir)
    days: set[str] = set()
    for root in (data / "review", data / "logs", data / "debug_logs"):
        if not root.exists():
            continue
        for path in root.rglob("*"):
            day = _date_from_path(path)
            if day:
                days.add(day)
    return sorted(days)[-1] if days else None


def _parse_no_action(row: dict[str, Any], line: str) -> None:
    kv = _parse_kv(line)
    detail_match = re.search(r"detail=(.+?)(?=\s+[A-Za-z_][A-Za-z0-9_]*=|$)", line)
    detail = detail_match.group(1).strip() if detail_match else line
    row["detail"] = detail
    row["final_skip_reason"] = "minimum_cash_to_deploy" if "minimum_cash_to_deploy" in detail else kv.get("reason")
    row["final_trade_size"] = (
        _round(kv.get("final_trade_size"), 2)
        or _round(kv.get("candidate_notional"), 2)
        or _extract_trade_size(detail)
    )
    row["minimum_cash_to_deploy"] = _round(kv.get("minimum_cash_to_deploy"), 2) or _extract_minimum_cash(detail)
    fields = {
        "available_cash": "available_cash",
        "gross_headroom": "gross_headroom",
        "candidate_notional_requested": "candidate_notional_requested",
        "candidate_requested_notional": "candidate_requested_notional",
        "candidate_notional": "candidate_notional",
        "candidate_notional_cap": "candidate_notional_cap",
        "base_requested_notional": "base_requested_notional",
        "tranche_min": "tranche_min",
        "target_allocation": "target_allocation",
        "current_dynamic_sleeve_usage": "current_dynamic_sleeve_usage",
        "dynamic_sleeve_cap": "dynamic_sleeve_cap",
        "min_order_notional": "min_order_notional",
        "max_single_dynamic_notional": "max_single_dynamic_notional",
    }
    for out_key, kv_key in fields.items():
        value = _round(kv.get(kv_key), 2)
        if value is not None:
            row[out_key] = value
    row["source"] = kv.get("source") or row.get("source")
    row["limiting_cap"] = kv.get("limiting_cap") or row.get("limiting_cap")
    row["existing_position"] = kv.get("position_already_held")
    row["raw_desired_notional"] = (
        row.get("candidate_notional_requested")
        or row.get("candidate_requested_notional")
        or row.get("base_requested_notional")
    )
    row["position_cap"] = row.get("position_cap") or row.get("target_allocation")
    row["symbol_cap"] = row.get("symbol_cap") or row.get("target_allocation")
    row["per_trade_cap"] = row.get("per_trade_cap") or row.get("max_single_dynamic_notional") or row.get("candidate_notional_cap")
    row["sleeve_allocation_cap"] = row.get("sleeve_allocation_cap") or row.get("dynamic_sleeve_cap")
    _mark_dynamic(row)


def _parse_size_trace(row: dict[str, Any], line: str) -> None:
    kv = _parse_kv(line)
    numeric_fields = {
        "candidate_rank": "candidate_rank",
        "score": "score",
        "strength": "strength",
        "account_equity": "account_equity",
        "available_cash": "available_cash",
        "gross_headroom": "gross_headroom",
        "raw_target_notional": "raw_target_notional",
        "target_pct": "target_pct",
        "dynamic_sleeve_cap": "dynamic_sleeve_cap",
        "core_sleeve_cap": "core_sleeve_cap",
        "sector_cap_remaining": "sector_cap_remaining",
        "symbol_cap_remaining": "symbol_cap_remaining",
        "position_cap_remaining": "position_cap_remaining",
        "per_trade_cap": "per_trade_cap",
        "max_trade_size": "max_trade_size",
        "after_sleeve_cap": "after_sleeve_cap",
        "after_sector_cap": "after_sector_cap",
        "after_symbol_cap": "after_symbol_cap",
        "after_position_cap": "after_position_cap",
        "after_gross_headroom": "after_gross_headroom",
        "final_trade_size": "final_trade_size",
        "minimum_cash_to_deploy": "minimum_cash_to_deploy",
        "min_realloc_leg": "min_order_notional",
    }
    trace: dict[str, Any] = {}
    for log_key, row_key in numeric_fields.items():
        value = _round(kv.get(log_key), 4)
        trace[log_key] = value
        if value is not None:
            row[row_key] = value
    for log_key, row_key in {
        "route": "route",
        "source": "source",
        "skip_reason": "final_skip_reason",
    }.items():
        value = kv.get(log_key)
        trace[log_key] = value
        if value:
            row[row_key] = value
    dyn = str(kv.get("dynamic_candidate") or "").strip().lower()
    if dyn in {"true", "t", "1", "yes"}:
        row["dynamic_candidate"] = True
        trace["dynamic_candidate"] = True
    elif dyn:
        trace["dynamic_candidate"] = False
    skipped = str(kv.get("skipped_by_min_deploy") or "").strip().lower()
    trace["skipped_by_min_deploy"] = skipped in {"true", "t", "1", "yes"}
    row["allocator_size_trace"] = trace
    row["raw_desired_notional"] = row.get("raw_target_notional") or row.get("raw_desired_notional")
    row["sleeve_allocation_cap"] = row.get("dynamic_sleeve_cap") or row.get("sleeve_allocation_cap")
    row["symbol_cap"] = row.get("symbol_cap_remaining") or row.get("symbol_cap")
    row["position_cap"] = row.get("position_cap_remaining") or row.get("position_cap")
    _mark_dynamic(row)


def _parse_logs(paths: Sequence[Path]) -> tuple[dict[str, dict[str, Any]], list[str]]:
    rows: dict[str, dict[str, Any]] = {}
    used: list[str] = []
    for path in paths:
        try:
            lines = _read_text(path).splitlines()
        except OSError:
            continue
        useful = False
        for line_no, line in enumerate(lines, start=1):
            entry = _ENTRY_EVAL_RE.search(line)
            if entry:
                row = _row(rows, entry.group("symbol"))
                row["route"] = entry.group("route")
                row["entry_eval_final"] = entry.group("final").lower() in {"t", "true"}
                row["entry_eval_reason"] = entry.group("reason").strip()
                _mark_dynamic(row)
                _note(row, path, line_no, line)
                useful = True
                continue
            if "ENTRY_TO_ALLOCATOR_TRACE" in line:
                kv = _parse_kv(line)
                if kv.get("symbol"):
                    row = _row(rows, kv["symbol"])
                    row["reached_allocator"] = True
                    row["route"] = kv.get("route") or row.get("route")
                    _mark_dynamic(row)
                    _note(row, path, line_no, line)
                    useful = True
                continue
            if re.search(r"\branked:\s*\[", line):
                for symbol in _parse_symbol_list(line, "ranked:"):
                    row = _row(rows, symbol)
                    row["ranked"] = True
                    _note(row, path, line_no, line)
                    useful = True
                continue
            if re.search(r"\bselected:\s*\[", line):
                for symbol in _parse_symbol_list(line, "selected:"):
                    row = _row(rows, symbol)
                    row["selected"] = True
                    _note(row, path, line_no, line)
                    useful = True
                continue
            if "ALLOCATOR_ACTIONS:" in line:
                try:
                    payload = ast.literal_eval(line.split("ALLOCATOR_ACTIONS:", 1)[1].strip())
                except Exception:
                    payload = None
                if isinstance(payload, list):
                    for action in payload:
                        if not isinstance(action, Mapping):
                            continue
                        symbol = str(action.get("symbol") or "").strip().upper()
                        if not symbol:
                            continue
                        row = _row(rows, symbol)
                        row["selected"] = True
                        row["source"] = action.get("source") or row.get("source")
                        row["final_trade_size"] = _round(action.get("notional"), 2) or row.get("final_trade_size")
                        _mark_dynamic(row)
                        _note(row, path, line_no, line)
                        useful = True
                continue
            if "ALLOCATOR_SIZE_TRACE" in line:
                kv = _parse_kv(line)
                symbol = kv.get("symbol")
                if symbol:
                    row = _row(rows, symbol)
                    _parse_size_trace(row, line)
                    _note(row, path, line_no, line)
                    useful = True
                continue
            if "ALLOCATOR_NO_ACTION_DETAIL" in line:
                kv = _parse_kv(line)
                symbol = kv.get("symbol")
                if symbol:
                    row = _row(rows, symbol)
                    _parse_no_action(row, line)
                    _note(row, path, line_no, line)
                    useful = True
                continue
            if "SKIP " in line and "minimum_cash_to_deploy" in line:
                match = _SKIP_RE.search(line)
                if match:
                    row = _row(rows, match.group("symbol"))
                    _parse_no_action(row, line)
                    _note(row, path, line_no, line)
                    useful = True
                continue
        if useful:
            used.append(str(path))
    return rows, used


def _sqlite_rows(data_dir: Path, *, day: str, user_id: str) -> dict[str, dict[str, Any]]:
    db_path = data_dir / "algo_live.db"
    rows: dict[str, dict[str, Any]] = {}
    if not db_path.exists():
        return rows
    try:
        con = sqlite3.connect(db_path)
    except sqlite3.Error:
        return rows
    try:
        query = "select ts,user_id,symbol,route,final,reason,payload_json from entry_evaluations where substr(ts,1,10)=?"
        args: list[Any] = [day]
        if user_id:
            query += " and user_id=?"
            args.append(user_id)
        for _ts, _uid, symbol, route, final, reason, _payload in con.execute(query, args):
            sym = str(symbol or "").strip().upper()
            if not sym:
                continue
            row = _row(rows, sym)
            row["route"] = route
            row["entry_eval_final"] = bool(final)
            row["entry_eval_reason"] = reason
            _mark_dynamic(row)
    except sqlite3.Error:
        pass
    finally:
        con.close()
    return rows


def _merge_rows(primary: dict[str, dict[str, Any]], secondary: Mapping[str, Mapping[str, Any]]) -> None:
    for symbol, raw in secondary.items():
        row = _row(primary, symbol)
        for key, value in raw.items():
            if key == "evidence":
                continue
            if row.get(key) in (None, False, [], "not_observed") and value not in (None, False, [], "not_observed"):
                row[key] = value
        _mark_dynamic(row)


def _is_dynamic_allocator_candidate(row: Mapping[str, Any]) -> bool:
    if row.get("dynamic_candidate"):
        return True
    if "dynamic" in str(row.get("route") or "").lower():
        return True
    if "dynamic" in str(row.get("source") or "").lower():
        return True
    return False


def _reached_allocator(row: Mapping[str, Any]) -> bool:
    return bool(
        row.get("reached_allocator")
        or row.get("ranked")
        or row.get("selected")
        or row.get("final_trade_size") is not None
        or row.get("minimum_cash_to_deploy") is not None
    )


def _nearly_equal(a: Any, b: Any, tol: float = 0.02) -> bool:
    af = _safe_float(a)
    bf = _safe_float(b)
    return af is not None and bf is not None and abs(af - bf) <= tol


def _clipping_steps(row: Mapping[str, Any]) -> tuple[list[str], str]:
    steps: list[str] = []
    trade = _safe_float(row.get("final_trade_size"))
    limiting = str(row.get("limiting_cap") or "none")
    trace_stage_map = (
        ("after_sleeve_cap", "dynamic_sleeve_cap"),
        ("after_sector_cap", "sector_cap"),
        ("after_symbol_cap", "symbol_cap"),
        ("after_position_cap", "position_cap"),
        ("after_gross_headroom", "gross_headroom"),
    )
    prev = _safe_float(row.get("raw_target_notional"))
    for field, label in trace_stage_map:
        current = _safe_float(row.get(field))
        if prev is not None and current is not None and current < prev - 0.01:
            steps.append(label)
        if current is not None:
            prev = current
    if limiting and limiting != "none":
        steps.append(limiting)
    if _nearly_equal(trade, row.get("candidate_notional")):
        steps.append("candidate_notional")
    if _nearly_equal(trade, row.get("candidate_notional_cap")):
        steps.append("candidate_notional_cap")
    if _nearly_equal(trade, row.get("base_requested_notional")):
        steps.append("base_requested_notional")
    if _nearly_equal(trade, row.get("tranche_min")):
        steps.append("rank_tranche_min")
    if row.get("minimum_cash_to_deploy") is not None and trade is not None:
        steps.append("minimum_cash_to_deploy_floor_check")
    seen = []
    for step in steps:
        if step not in seen:
            seen.append(step)
    formula = "not_observed"
    non_floor_steps = [step for step in steps if step != "minimum_cash_to_deploy_floor_check"]
    if non_floor_steps:
        formula = non_floor_steps[-1]
    elif _nearly_equal(trade, row.get("candidate_notional_cap")):
        formula = "candidate_notional_cap"
    elif _nearly_equal(trade, row.get("candidate_notional")):
        formula = "candidate_notional_after_allocator_caps"
    elif _nearly_equal(trade, row.get("base_requested_notional")):
        formula = "rank_tranche_or_per_trade_target"
    elif _nearly_equal(trade, row.get("tranche_min")):
        formula = "rank_tranche_min"
    elif limiting and limiting != "none":
        formula = limiting
    return seen, formula


def _first_clipping_stage(row: Mapping[str, Any]) -> str | None:
    prev = _safe_float(row.get("raw_target_notional"))
    for field, label in (
        ("after_sleeve_cap", "dynamic_sleeve_cap"),
        ("after_sector_cap", "sector_cap"),
        ("after_symbol_cap", "symbol_cap"),
        ("after_position_cap", "position_cap"),
        ("after_gross_headroom", "gross_headroom"),
        ("final_trade_size", "final_trade_size"),
    ):
        current = _safe_float(row.get(field))
        if prev is not None and current is not None and current < prev - 0.01:
            return label
        if current is not None:
            prev = current
    limiting = str(row.get("limiting_cap") or "").strip()
    return limiting if limiting and limiting != "none" else None


def _first_below_min_deploy_stage(row: Mapping[str, Any]) -> str | None:
    minimum = _safe_float(row.get("minimum_cash_to_deploy"))
    if minimum is None:
        return None
    for field, label in (
        ("raw_target_notional", "raw_target_notional"),
        ("after_sleeve_cap", "dynamic_sleeve_cap"),
        ("after_sector_cap", "sector_cap"),
        ("after_symbol_cap", "symbol_cap"),
        ("after_position_cap", "position_cap"),
        ("after_gross_headroom", "gross_headroom"),
        ("final_trade_size", "final_trade_size"),
    ):
        current = _safe_float(row.get(field))
        if current is not None and current + _MIN_DEPLOY_BUFFER < minimum:
            return label
    return None


def _finalize(row: dict[str, Any]) -> dict[str, Any]:
    steps, formula = _clipping_steps(row)
    row["clipping_steps_detected"] = steps
    first_clip = _first_clipping_stage(row)
    row["sizing_formula_inference"] = first_clip or formula
    row["first_clipping_stage"] = first_clip
    row["first_below_min_deploy_stage"] = _first_below_min_deploy_stage(row)
    if row.get("final_skip_reason") is None and row.get("minimum_cash_to_deploy") is not None:
        row["final_skip_reason"] = "minimum_cash_to_deploy"
    trade = _safe_float(row.get("final_trade_size"))
    minimum = _safe_float(row.get("minimum_cash_to_deploy"))
    row["trade_size_to_minimum_cash_ratio"] = (
        round(trade / minimum, 4) if trade is not None and minimum not in (None, 0.0) else None
    )
    row["systematically_below_minimum_floor"] = (
        bool(minimum is not None and trade is not None and trade + _MIN_DEPLOY_BUFFER < minimum)
    )
    _mark_dynamic(row)
    return row


def _mean(values: Sequence[float]) -> float | None:
    vals = [float(v) for v in values if math.isfinite(float(v))]
    return round(sum(vals) / len(vals), 4) if vals else None


def _median(values: Sequence[float]) -> float | None:
    vals = sorted(float(v) for v in values if math.isfinite(float(v)))
    return round(median(vals), 4) if vals else None


def _would_clear(row: Mapping[str, Any], threshold: float) -> bool:
    trade = _safe_float(row.get("final_trade_size")) or 0.0
    return trade + _MIN_DEPLOY_BUFFER >= max(0.0, float(threshold))


def _what_if(candidates: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    rows = [row for row in candidates if _safe_float(row.get("final_trade_size")) is not None]

    def block_for(label: str, threshold_fn: Any) -> dict[str, Any]:
        cleared = []
        blocked = []
        for row in rows:
            threshold = float(threshold_fn(row))
            if _would_clear(row, threshold):
                cleared.append(row)
            else:
                blocked.append(row)
        return {
            "threshold": label,
            "would_clear_count": len(cleared),
            "would_remain_blocked_count": len(blocked),
            "would_clear_symbols": sorted(str(row["symbol"]) for row in cleared),
            "would_remain_blocked_symbols": sorted(str(row["symbol"]) for row in blocked),
        }

    current = lambda row: _safe_float(row.get("minimum_cash_to_deploy")) or 0.0
    min_leg = lambda row: _safe_float(row.get("min_order_notional")) or 0.0
    no_floor = lambda _row: 0.0
    max_deficit = max(
        (
            max(0.0, (_safe_float(row.get("minimum_cash_to_deploy")) or 0.0) - _MIN_DEPLOY_BUFFER - (_safe_float(row.get("final_trade_size")) or 0.0))
            for row in rows
        ),
        default=0.0,
    )
    return {
        "current_behavior": block_for("current", current),
        "lower_minimum_cash_to_deploy_25pct": block_for("75pct_floor", lambda row: current(row) * 0.75),
        "lower_minimum_cash_to_deploy_50pct": block_for("50pct_floor", lambda row: current(row) * 0.50),
        "lower_minimum_cash_to_deploy_75pct": block_for("25pct_floor", lambda row: current(row) * 0.25),
        "min_realloc_leg_only": block_for("min_realloc_leg", min_leg),
        "no_minimum_deployment_floor": block_for("no_floor", no_floor),
        "raise_dynamic_per_trade_target_enough_to_clear_floor": {
            "minimum_extra_trade_size_needed": round(max_deficit, 2),
            "target_trade_size_needed_by_symbol": {
                str(row["symbol"]): round(max(0.0, (current(row) - _MIN_DEPLOY_BUFFER)), 2)
                for row in rows
            },
        },
    }


def _explain_symbol(symbol: str, row: Mapping[str, Any] | None) -> str:
    if not row:
        return f"{symbol}: no allocator sizing evidence found."
    trade = row.get("final_trade_size")
    min_cash = row.get("minimum_cash_to_deploy")
    formula = row.get("sizing_formula_inference")
    if symbol == "INTC" and trade is not None:
        return (
            f"INTC final_trade_size={trade} vs minimum_cash_to_deploy={min_cash}. "
            f"The parsed allocator detail points to {formula}; candidate_notional={row.get('candidate_notional')}, "
            f"base_requested_notional={row.get('base_requested_notional')}, tranche_min={row.get('tranche_min')}, "
            f"candidate_notional_cap={row.get('candidate_notional_cap')}, limiting_cap={row.get('limiting_cap')}, "
            f"first_clipping_stage={row.get('first_clipping_stage')}, "
            f"first_below_min_deploy_stage={row.get('first_below_min_deploy_stage')}. "
            "If trace fields are present, the first clipping stage is the first sizing step that reduced the target."
        )
    return (
        f"{symbol}: trade_size={trade} min_cash={min_cash} formula={formula} "
        f"skip={row.get('final_skip_reason')} steps={row.get('clipping_steps_detected')}"
    )


def build_dynamic_allocator_sizing_report(
    *,
    project_root: Path | str = ".",
    data_dir: Path | str = "data",
    day: str,
    user_id: str = "paper_bot",
    log_paths: Sequence[Path | str] | None = None,
) -> dict[str, Any]:
    root = Path(project_root)
    data = Path(data_dir)
    if day == "latest":
        day = latest_dynamic_allocator_sizing_date(project_root=root, data_dir=data, user_id=user_id) or day
    discovered = _discover_log_paths(root, data_dir=data, day=day, extra_paths=log_paths)
    rows, used = _parse_logs(discovered)
    _merge_rows(rows, _sqlite_rows(data, day=day, user_id=user_id))
    all_rows = [_finalize(dict(row)) for row in rows.values()]
    repeated_trade_sizes = Counter(
        round(float(v), 2)
        for row in all_rows
        if (v := _safe_float(row.get("final_trade_size"))) is not None
    )
    for row in all_rows:
        trade = _safe_float(row.get("final_trade_size"))
        if (
            trade is not None
            and row.get("sizing_formula_inference") == "not_observed"
            and repeated_trade_sizes.get(round(float(trade), 2), 0) > 1
        ):
            row["sizing_formula_inference"] = "repeated_allocator_tranche_or_per_trade_target_observed"
            steps = list(row.get("clipping_steps_detected") or [])
            if "observed_repeated_trade_size" not in steps:
                steps.insert(0, "observed_repeated_trade_size")
            row["clipping_steps_detected"] = steps
    dynamic_candidates = [
        row for row in all_rows if _is_dynamic_allocator_candidate(row) and _reached_allocator(row)
    ]
    dynamic_candidates.sort(key=lambda row: row["symbol"])
    trade_sizes = [float(v) for row in dynamic_candidates if (v := _safe_float(row.get("final_trade_size"))) is not None]
    minimums = [float(v) for row in dynamic_candidates if (v := _safe_float(row.get("minimum_cash_to_deploy"))) is not None]
    ratios = [
        float(v) for row in dynamic_candidates if (v := _safe_float(row.get("trade_size_to_minimum_cash_ratio"))) is not None
    ]
    clipping_counts = Counter()
    for row in dynamic_candidates:
        steps = row.get("clipping_steps_detected") or ["not_observed"]
        clipping_counts.update(steps)
    symbols_most_affected = Counter(
        str(row["symbol"])
        for row in dynamic_candidates
        if row.get("systematically_below_minimum_floor")
    )
    focus_map = {symbol: next((row for row in all_rows if row["symbol"] == symbol), None) for symbol in _FOCUS_SYMBOLS}
    return {
        "report": "dynamic_allocator_sizing",
        "research_only": True,
        "date": day,
        "user": user_id,
        "source_files": used,
        "summary": {
            "dynamic_candidates_reaching_allocator": len(dynamic_candidates),
            "skipped_by_min_deploy_floor": sum(1 for row in dynamic_candidates if row.get("systematically_below_minimum_floor")),
            "average_trade_size": _mean(trade_sizes),
            "median_trade_size": _median(trade_sizes),
            "average_minimum_cash_to_deploy": _mean(minimums),
            "average_trade_size_to_minimum_cash_ratio": _mean(ratios),
            "most_common_clipping_source": clipping_counts.most_common(1)[0][0] if clipping_counts else None,
            "clipping_source_counts": dict(clipping_counts.most_common()),
            "symbols_most_affected": dict(symbols_most_affected.most_common()),
        },
        "dynamic_candidates": dynamic_candidates,
        "focus_symbols": focus_map,
        "explanations": {symbol: _explain_symbol(symbol, row) for symbol, row in focus_map.items()},
        "what_if": _what_if(dynamic_candidates),
    }


def render_dynamic_allocator_sizing_report(report: Mapping[str, Any]) -> str:
    summary = report.get("summary") if isinstance(report.get("summary"), Mapping) else {}
    lines = [
        f"Dynamic Allocator Sizing Report - {report.get('date')} user={report.get('user')}",
        "Research-only: no trading behavior, config, allocator, dispatch, scanner, or risk model changed.",
        "",
        "Summary",
        f"- source_files: {len(report.get('source_files') or [])}",
        f"- dynamic_candidates_reaching_allocator: {summary.get('dynamic_candidates_reaching_allocator', 0)}",
        f"- skipped_by_min_deploy_floor: {summary.get('skipped_by_min_deploy_floor', 0)}",
        f"- average_trade_size: {summary.get('average_trade_size')}",
        f"- median_trade_size: {summary.get('median_trade_size')}",
        f"- average_minimum_cash_to_deploy: {summary.get('average_minimum_cash_to_deploy')}",
        f"- average_trade_size_to_minimum_cash_ratio: {summary.get('average_trade_size_to_minimum_cash_ratio')}",
        f"- most_common_clipping_source: {summary.get('most_common_clipping_source')}",
        f"- clipping_source_counts: {summary.get('clipping_source_counts')}",
        f"- symbols_most_affected: {summary.get('symbols_most_affected')}",
        "",
        "Focus Symbols",
    ]
    explanations = report.get("explanations") if isinstance(report.get("explanations"), Mapping) else {}
    for symbol in _FOCUS_SYMBOLS:
        lines.append(f"- {explanations.get(symbol)}")
    lines.extend(["", "What If"])
    what_if = report.get("what_if") if isinstance(report.get("what_if"), Mapping) else {}
    for key, block in what_if.items():
        lines.append(f"- {key}: {block}")
    lines.extend(["", "Dynamic Candidates"])
    rows = report.get("dynamic_candidates") if isinstance(report.get("dynamic_candidates"), list) else []
    if not rows:
        lines.append("- none")
    for row in rows[:150]:
        lines.append(
            "- {symbol} route={route} source={source} entry_final={entry_eval_final} ranked={ranked} selected={selected} "
            "raw_desired={raw_desired_notional} trade_size={final_trade_size} min_cash={minimum_cash_to_deploy} "
            "available_cash={available_cash} gross_headroom={gross_headroom} position_cap={position_cap} "
            "per_trade_cap={per_trade_cap} sleeve_cap={sleeve_allocation_cap} existing={existing_position} "
            "formula={sizing_formula_inference} first_clip={first_clipping_stage} "
            "first_below_min={first_below_min_deploy_stage} steps={clipping_steps_detected} "
            "skip={final_skip_reason}".format(**row)
        )
    return "\n".join(lines) + "\n"


def write_dynamic_allocator_sizing_report(
    *,
    project_root: Path | str = ".",
    data_dir: Path | str = "data",
    day: str,
    user_id: str = "paper_bot",
    log_paths: Sequence[Path | str] | None = None,
) -> tuple[Path, Path, dict[str, Any]]:
    data = Path(data_dir)
    report = build_dynamic_allocator_sizing_report(
        project_root=project_root,
        data_dir=data,
        day=day,
        user_id=user_id,
        log_paths=log_paths,
    )
    out_dir = data / "research" / "dynamic_allocator_sizing"
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{report['date']}_{_safe_user(user_id)}"
    json_path = out_dir / f"{stem}.json"
    text_path = out_dir / f"{stem}.txt"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    text_path.write_text(render_dynamic_allocator_sizing_report(report), encoding="utf-8")
    return json_path, text_path, report
