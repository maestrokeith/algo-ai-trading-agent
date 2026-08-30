"""Research-only allocator versus dispatch mismatch diagnostics."""

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
from typing import Any

_ISO_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
_COMPACT_DATE_RE = re.compile(r"(\d{8})")
_KV_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=([^ ]+)")
_ENTRY_EVAL_RE = re.compile(
    r"\b(?P<symbol>[A-Z][A-Z0-9.\-]{0,9})\s+ENTRY_EVAL\s+"
    r"route=(?P<route>[^ ]+).*?\bfinal=(?P<final>[TF]|true|false|True|False)\b.*?"
    r"\breason=(?P<reason>.+?)(?=\s+[A-Za-z_][A-Za-z0-9_]*=|$)"
)
_SKIP_RE = re.compile(
    r"\bSKIP\s+(?P<symbol>[A-Z][A-Z0-9.\-]{0,9}):\s+"
    r"(?:(?:reason=(?P<reason>.+?)\s+detail=)|(?P<detail_only>.*))"
    r"(?P<detail>.*)$"
)
_FOCUS_SYMBOLS = ("INTC", "AMD", "NOK", "IWM", "XLF", "XLE", "QQQ")


@dataclass(frozen=True)
class AllocatorDispatchMismatchPaths:
    json_path: Path
    text_path: Path


def _safe_user(user_id: str) -> str:
    return "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in str(user_id or "default")) or "default"


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip().strip("$,")
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


def _parse_symbol_list(line: str, *, marker: str | None = None) -> list[str]:
    text = line
    if marker and marker in line:
        text = line.split(marker, 1)[1]
    body_match = re.search(r"\[(?P<body>[^\]]*)\]", text)
    if not body_match:
        return []
    body = "[" + body_match.group("body") + "]"
    try:
        parsed = ast.literal_eval(body)
    except Exception:
        parsed = None
    if isinstance(parsed, list):
        return [str(item).strip().upper() for item in parsed if str(item).strip()]
    return [symbol.upper() for symbol in re.findall(r"[A-Z][A-Z0-9.\-]{0,9}", body)]


def _parse_actions(line: str) -> list[dict[str, Any]]:
    if "ALLOCATOR ACTIONS:" not in line:
        return []
    payload = line.split("ALLOCATOR ACTIONS:", 1)[1].strip()
    try:
        parsed = ast.literal_eval(payload)
    except Exception:
        parsed = None
    actions: list[dict[str, Any]] = []
    if isinstance(parsed, list):
        for raw in parsed:
            if not isinstance(raw, Mapping):
                continue
            symbol = str(raw.get("symbol") or "").strip().upper()
            if not symbol:
                continue
            actions.append(
                {
                    "action": raw.get("action"),
                    "symbol": symbol,
                    "notional": _round(raw.get("notional"), 2),
                    "source": raw.get("source"),
                }
            )
        return actions
    for symbol in re.findall(r"'symbol':\s*'([A-Z][A-Z0-9.\-]{0,9})'|symbol[=:]'?([A-Z][A-Z0-9.\-]{0,9})", payload):
        sym = (symbol[0] or symbol[1]).upper()
        start = max(payload.find(sym) - 80, 0)
        end = min(payload.find(sym) + 180, len(payload))
        chunk = payload[start:end]
        notional_match = re.search(r"notional['=:\s]+([0-9]+(?:\.[0-9]+)?)", chunk)
        source_match = re.search(r"source['=:\s]+['\"]?([A-Za-z0-9_\-]+)", chunk)
        actions.append(
            {
                "action": "buy" if "buy" in chunk else None,
                "symbol": sym,
                "notional": _round(notional_match.group(1) if notional_match else None, 2),
                "source": source_match.group(1) if source_match else None,
            }
        )
    return actions


def _extract_trade_size(detail: str) -> float | None:
    match = re.search(r"trade_size\s+\$?([0-9]+(?:\.[0-9]+)?)", detail)
    return _round(match.group(1), 2) if match else None


def _extract_minimum_cash(detail: str) -> float | None:
    match = re.search(r"minimum_cash_to_deploy\s+([0-9]+(?:\.[0-9]+)?)", detail)
    return _round(match.group(1), 2) if match else None


def _blank_candidate(symbol: str) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "route": None,
        "dynamic_candidate": False,
        "source": None,
        "entry_eval_final": None,
        "entry_eval_reason": None,
        "reached_allocator_trace": False,
        "allocator_ranked": False,
        "allocator_selected": False,
        "allocator_action": False,
        "action": None,
        "proposed_notional": None,
        "trade_size": None,
        "minimum_cash_to_deploy": None,
        "gross_headroom": None,
        "available_cash": None,
        "limiting_cap": None,
        "dispatch_started": False,
        "dispatch_result": None,
        "dispatch_skip_reason": None,
        "dispatch_dynamic_rvol_check": None,
        "dispatch_dynamic_missing_fields": [],
        "dispatch_dynamic_available_keys": [],
        "dispatch_dynamic_skip_detail": None,
        "order_intent_produced": False,
        "final_pipeline_outcome": "unknown",
        "mismatch_categories": [],
        "evidence": [],
    }


def _candidate(rows: dict[str, dict[str, Any]], symbol: str) -> dict[str, Any]:
    sym = symbol.strip().upper()
    if sym not in rows:
        rows[sym] = _blank_candidate(sym)
    return rows[sym]


def _note(row: dict[str, Any], path: Path, line_no: int, line: str) -> None:
    if len(row["evidence"]) >= 12:
        return
    row["evidence"].append({"source_file": str(path), "line_number": line_no, "line": line.strip()})


def _mark_dynamic(row: dict[str, Any]) -> None:
    route = str(row.get("route") or "").lower()
    source = str(row.get("source") or "").lower()
    if "dynamic" in route or "dynamic" in source:
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


def latest_allocator_dispatch_mismatch_date(
    *,
    project_root: Path | str = ".",
    data_dir: Path | str = "data",
    user_id: str = "paper_bot",
) -> str | None:
    del project_root, user_id
    data = Path(data_dir)
    days: set[str] = set()
    review_root = data / "review"
    if review_root.exists():
        for path in review_root.glob("*"):
            day = _date_from_path(path)
            if day:
                days.add(day)
    for root in (data / "logs", data / "debug_logs"):
        if root.exists():
            for path in root.rglob("*"):
                if path.is_file() and (day := _date_from_path(path)):
                    days.add(day)
    return sorted(days)[-1] if days else None


def _parse_no_action(row: dict[str, Any], line: str) -> None:
    kv = _parse_kv(line)
    detail_match = re.search(r"detail=(.+?)(?=\s+[A-Za-z_][A-Za-z0-9_]*=|$)", line)
    detail = detail_match.group(1).strip() if detail_match else line
    row["trade_size"] = _round(kv.get("final_trade_size") or kv.get("candidate_notional"), 2) or _extract_trade_size(detail)
    row["minimum_cash_to_deploy"] = _round(kv.get("minimum_cash_to_deploy"), 2) or _extract_minimum_cash(detail)
    row["available_cash"] = _round(kv.get("available_cash"), 2)
    row["gross_headroom"] = _round(kv.get("gross_headroom"), 2)
    row["limiting_cap"] = kv.get("limiting_cap") or row.get("limiting_cap")
    row["source"] = kv.get("source") or row.get("source")
    row["dispatch_result"] = row.get("dispatch_result") or "not_started"
    _mark_dynamic(row)


def _parse_optional_float(value: Any) -> float | None:
    text = str(value or "").strip()
    if text.lower() in {"", "n/a", "none", "null"}:
        return None
    return _round(text, 4)


def _parse_bool_text(value: Any) -> bool | None:
    text = str(value or "").strip().lower()
    if text in {"true", "t", "1", "yes", "on"}:
        return True
    if text in {"false", "f", "0", "no", "off"}:
        return False
    return None


def _parse_csv_field(value: Any) -> list[str]:
    text = str(value or "").strip()
    if not text or text.lower() in {"none", "n/a"}:
        return []
    return [item for item in (part.strip() for part in text.split(",")) if item]


def _parse_dispatch_dynamic_rvol_check(row: dict[str, Any], line: str) -> None:
    kv = _parse_kv(line)
    check = {
        "route": kv.get("route"),
        "source": kv.get("source"),
        "dynamic_candidate": _parse_bool_text(kv.get("dynamic_candidate")),
        "rel_volume": _parse_optional_float(kv.get("rel_volume")),
        "base_min_rel_volume": _parse_optional_float(kv.get("base_min_rel_volume")),
        "effective_min_rel_volume": _parse_optional_float(kv.get("effective_min_rel_volume")),
        "override_active": _parse_bool_text(kv.get("override_active")),
        "news_score": _parse_optional_float(kv.get("news_score")),
        "catalyst_score": _parse_optional_float(kv.get("catalyst_score")),
        "event_score": _parse_optional_float(kv.get("event_score")),
        "catalyst_type": kv.get("catalyst_type"),
        "catalyst_age_minutes": _parse_optional_float(kv.get("catalyst_age_minutes")),
        "scanner_effective_min_rel_volume": _parse_optional_float(kv.get("scanner_effective_min_rel_volume")),
        "entry_eval_route": kv.get("entry_eval_route"),
        "decision_allowed": kv.get("decision_allowed"),
        "dispatch_result": kv.get("dispatch_result"),
        "dispatch_reason": kv.get("dispatch_reason"),
    }
    row["dispatch_dynamic_rvol_check"] = check
    row["route"] = check.get("entry_eval_route") or check.get("route") or row.get("route")
    row["source"] = check.get("source") or row.get("source")
    if check.get("dynamic_candidate") is True:
        row["dynamic_candidate"] = True
    if check.get("rel_volume") is not None:
        row["dispatch_rel_volume"] = check.get("rel_volume")
    if check.get("effective_min_rel_volume") is not None:
        row["dispatch_effective_min_rel_volume"] = check.get("effective_min_rel_volume")
    if check.get("dispatch_result") == "skipped":
        row["dispatch_result"] = "skipped"
        row["dispatch_skip_reason"] = check.get("dispatch_reason") or row.get("dispatch_skip_reason")
    _mark_dynamic(row)


def _parse_dispatch_dynamic_missing(row: dict[str, Any], line: str) -> None:
    kv = _parse_kv(line)
    row["dispatch_dynamic_missing_fields"] = _parse_csv_field(kv.get("missing_fields"))
    row["dispatch_dynamic_available_keys"] = _parse_csv_field(kv.get("available_keys"))


def _parse_dispatch_dynamic_skip_detail(row: dict[str, Any], line: str) -> None:
    kv = _parse_kv(line)
    detail = {
        "threshold_used": _parse_optional_float(kv.get("threshold_used")),
        "base_min_rel_volume": _parse_optional_float(kv.get("base_min_rel_volume")),
        "rel_volume": _parse_optional_float(kv.get("rel_volume")),
        "override_active": _parse_bool_text(kv.get("override_active")),
        "override_reason": kv.get("override_reason"),
        "missing_fields": _parse_csv_field(kv.get("missing_fields")),
        "dispatch_reason": kv.get("dispatch_reason"),
    }
    row["dispatch_dynamic_skip_detail"] = detail
    row["dispatch_result"] = "skipped"
    row["dispatch_skip_reason"] = detail.get("dispatch_reason") or row.get("dispatch_skip_reason")


def _parse_logs(paths: Sequence[Path], *, day: str) -> tuple[dict[str, dict[str, Any]], list[str]]:
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
                row = _candidate(rows, entry.group("symbol"))
                final_raw = entry.group("final").lower()
                row["route"] = entry.group("route")
                row["entry_eval_final"] = final_raw in {"t", "true"}
                row["entry_eval_reason"] = entry.group("reason").strip()
                _mark_dynamic(row)
                _note(row, path, line_no, line)
                useful = True
                continue
            if "ENTRY_TO_ALLOCATOR_TRACE" in line:
                kv = _parse_kv(line)
                symbol = kv.get("symbol")
                if symbol:
                    row = _candidate(rows, symbol)
                    row["reached_allocator_trace"] = True
                    row["route"] = kv.get("route") or row.get("route")
                    _mark_dynamic(row)
                    _note(row, path, line_no, line)
                    useful = True
                continue
            if re.search(r"\branked:\s*\[", line):
                for symbol in _parse_symbol_list(line, marker="ranked:"):
                    row = _candidate(rows, symbol)
                    row["allocator_ranked"] = True
                    _note(row, path, line_no, line)
                    useful = True
                continue
            if re.search(r"\bselected:\s*\[", line):
                for symbol in _parse_symbol_list(line, marker="selected:"):
                    row = _candidate(rows, symbol)
                    row["allocator_selected"] = True
                    _note(row, path, line_no, line)
                    useful = True
                continue
            if "ALLOCATOR_NO_ACTION_DETAIL" in line:
                kv = _parse_kv(line)
                symbol = kv.get("symbol")
                if symbol:
                    row = _candidate(rows, symbol)
                    _parse_no_action(row, line)
                    _note(row, path, line_no, line)
                    useful = True
                continue
            if "SKIP " in line and "minimum_cash_to_deploy" in line:
                skip = _SKIP_RE.search(line)
                if skip:
                    row = _candidate(rows, skip.group("symbol"))
                    _parse_no_action(row, line)
                    _note(row, path, line_no, line)
                    useful = True
                continue
            actions = _parse_actions(line)
            if actions:
                for action in actions:
                    row = _candidate(rows, str(action["symbol"]))
                    row["allocator_action"] = True
                    row["allocator_selected"] = True
                    row["action"] = action.get("action")
                    row["proposed_notional"] = action.get("notional") or row.get("proposed_notional")
                    row["source"] = action.get("source") or row.get("source")
                    _mark_dynamic(row)
                    _note(row, path, line_no, line)
                useful = True
                continue
            if "ALLOCATOR_DISPATCH_START" in line:
                kv = _parse_kv(line)
                symbol = kv.get("symbol")
                if symbol:
                    row = _candidate(rows, symbol)
                    row["dispatch_started"] = True
                    row["action"] = kv.get("action") or row.get("action")
                    row["proposed_notional"] = _round(kv.get("notional"), 2) or row.get("proposed_notional")
                    row["source"] = kv.get("source") or row.get("source")
                    _mark_dynamic(row)
                    _note(row, path, line_no, line)
                    useful = True
                continue
            if "DISPATCH_DYNAMIC_RVOL_CHECK" in line:
                kv = _parse_kv(line)
                symbol = kv.get("symbol")
                if symbol:
                    row = _candidate(rows, symbol)
                    _parse_dispatch_dynamic_rvol_check(row, line)
                    _note(row, path, line_no, line)
                    useful = True
                continue
            if "DISPATCH_DYNAMIC_METADATA_MISSING" in line:
                kv = _parse_kv(line)
                symbol = kv.get("symbol")
                if symbol:
                    row = _candidate(rows, symbol)
                    _parse_dispatch_dynamic_missing(row, line)
                    _note(row, path, line_no, line)
                    useful = True
                continue
            if "DISPATCH_DYNAMIC_RVOL_SKIP_DETAIL" in line:
                kv = _parse_kv(line)
                symbol = kv.get("symbol")
                if symbol:
                    row = _candidate(rows, symbol)
                    _parse_dispatch_dynamic_skip_detail(row, line)
                    _note(row, path, line_no, line)
                    useful = True
                continue
            if "ALLOCATOR_DISPATCH_SKIPPED" in line or "ALLOCATOR_ACTION_BLOCKED" in line:
                kv = _parse_kv(line)
                symbol = kv.get("symbol")
                if symbol:
                    row = _candidate(rows, symbol)
                    row["dispatch_result"] = "skipped"
                    row["dispatch_skip_reason"] = kv.get("reason")
                    _mark_dynamic(row)
                    _note(row, path, line_no, line)
                    useful = True
                continue
            if "ORDER_INTENT" in line or "ALLOCATOR_ORDER_INTENT" in line:
                kv = _parse_kv(line)
                symbol = kv.get("symbol")
                if symbol:
                    row = _candidate(rows, symbol)
                    row["order_intent_produced"] = True
                    _note(row, path, line_no, line)
                    useful = True
                continue
            if "ALLOCATOR_DISPATCH_DONE" in line or "ALLOCATOR_DISPATCH_END" in line:
                kv = _parse_kv(line)
                symbol = kv.get("symbol")
                if symbol:
                    row = _candidate(rows, symbol)
                    result = kv.get("result")
                    reason = kv.get("reason")
                    row["dispatch_result"] = result or row.get("dispatch_result")
                    if result == "skipped":
                        row["dispatch_skip_reason"] = reason or row.get("dispatch_skip_reason")
                    if result == "submitted":
                        row["order_intent_produced"] = True
                    _note(row, path, line_no, line)
                    useful = True
                continue
        if useful:
            used.append(str(path))
    return rows, used


def _sqlite_rows(data_dir: Path, *, day: str, user_id: str) -> dict[str, dict[str, Any]]:
    db_path = data_dir / "algo_live.db"
    if not db_path.exists():
        return {}
    rows: dict[str, dict[str, Any]] = {}
    try:
        con = sqlite3.connect(db_path)
    except sqlite3.Error:
        return rows
    try:
        query = "select ts,user_id,symbol,route,final,reason,payload_json from entry_evaluations"
        clauses = ["substr(ts,1,10)=?"]
        args: list[Any] = [day]
        if user_id:
            clauses.append("user_id=?")
            args.append(user_id)
        for _ts, _uid, symbol, route, final, reason, _payload in con.execute(query + " where " + " and ".join(clauses), args):
            sym = str(symbol or "").upper()
            if not sym:
                continue
            row = _candidate(rows, sym)
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
        row = _candidate(primary, symbol)
        for key, value in raw.items():
            if key == "evidence":
                continue
            if row.get(key) in (None, False, [], "unknown") and value not in (None, False, [], "unknown"):
                row[key] = value
        _mark_dynamic(row)


def _reaches_allocator(row: Mapping[str, Any]) -> bool:
    return any(
        bool(row.get(key))
        for key in (
            "reached_allocator_trace",
            "allocator_ranked",
            "allocator_selected",
            "allocator_action",
            "dispatch_started",
            "dispatch_skip_reason",
            "minimum_cash_to_deploy",
        )
    )


def _finalize(row: dict[str, Any]) -> dict[str, Any]:
    categories: set[str] = set()
    if row.get("allocator_selected") and row.get("dispatch_result") == "skipped":
        categories.add("allocator_selected_dispatch_skipped")
    if row.get("entry_eval_final") is True and row.get("minimum_cash_to_deploy") is not None:
        categories.add("entry_eval_passed_allocator_min_cash_skip")
    if row.get("allocator_selected") and not row.get("order_intent_produced") and not row.get("dispatch_started"):
        categories.add("allocator_selected_no_order_intent")
    check = row.get("dispatch_dynamic_rvol_check") if isinstance(row.get("dispatch_dynamic_rvol_check"), Mapping) else {}
    scanner_effective = _safe_float(check.get("scanner_effective_min_rel_volume")) if check else None
    threshold_used = None
    skip_detail = row.get("dispatch_dynamic_skip_detail") if isinstance(row.get("dispatch_dynamic_skip_detail"), Mapping) else {}
    if skip_detail:
        threshold_used = _safe_float(skip_detail.get("threshold_used"))
    if threshold_used is None and check:
        threshold_used = _safe_float(check.get("effective_min_rel_volume"))
    rel_volume = _safe_float(check.get("rel_volume")) if check else None
    scanner_override_missing_or_not_applied = False
    if row.get("dispatch_skip_reason") == "dynamic_relative_volume" and check:
        missing = set(row.get("dispatch_dynamic_missing_fields") or [])
        scanner_override_missing_or_not_applied = (
            bool(missing.intersection({"effective_min_rel_volume", "news_score", "catalyst_score", "event_score"}))
            or check.get("override_active") is False
            or (
                scanner_effective is not None
                and threshold_used is not None
                and threshold_used > scanner_effective + 1e-9
                and rel_volume is not None
                and rel_volume >= scanner_effective - 1e-9
            )
        )
    if row.get("dispatch_skip_reason") == "dynamic_relative_volume" and (
        row.get("dynamic_candidate") or "dynamic" in str(row.get("source") or "").lower()
    ) and (not check or scanner_override_missing_or_not_applied):
        categories.add("scanner_override_not_honored_at_dispatch")
    trade_size = _safe_float(row.get("trade_size"))
    min_cash = _safe_float(row.get("minimum_cash_to_deploy"))
    if row.get("allocator_selected") and trade_size is not None and min_cash is not None and trade_size + 5.0 < min_cash:
        categories.add("selected_size_clipped_below_min_cash")
    if row.get("dispatch_result") == "skipped":
        row["final_pipeline_outcome"] = f"dispatch_skipped:{row.get('dispatch_skip_reason') or 'unknown'}"
    elif row.get("order_intent_produced"):
        row["final_pipeline_outcome"] = "order_intent_produced"
    elif row.get("minimum_cash_to_deploy") is not None:
        row["final_pipeline_outcome"] = "allocator_min_cash_skip"
    elif row.get("allocator_selected") and not row.get("dispatch_started"):
        row["final_pipeline_outcome"] = "allocator_selected_no_dispatch"
    elif row.get("entry_eval_final") is True and not row.get("allocator_selected"):
        row["final_pipeline_outcome"] = "entry_eval_passed_no_allocator_action"
    row["mismatch_categories"] = sorted(categories)
    _mark_dynamic(row)
    return row


def _explain_symbol(symbol: str, row: Mapping[str, Any] | None) -> str:
    if not row:
        return f"{symbol}: no allocator-stage evidence found in local logs."
    if symbol == "INTC" and row.get("minimum_cash_to_deploy") is not None:
        cap = row.get("limiting_cap") or "not visible in parsed logs"
        return (
            "INTC passed entry evaluation and reached allocator, but allocator sizing was below the deployment floor: "
            f"trade_size={row.get('trade_size')} minimum_cash_to_deploy={row.get('minimum_cash_to_deploy')} "
            f"gross_headroom={row.get('gross_headroom')} available_cash={row.get('available_cash')} limiting_cap={cap}."
        )
    if symbol == "AMD" and row.get("dispatch_skip_reason"):
        check = row.get("dispatch_dynamic_rvol_check") if isinstance(row.get("dispatch_dynamic_rvol_check"), Mapping) else {}
        missing = ",".join(row.get("dispatch_dynamic_missing_fields") or []) or "none"
        override_visibility = (
            "visible"
            if any(
                "override" in str(item.get("line", "")).lower()
                for item in row.get("evidence") or []
                if "ALLOCATOR_DISPATCH" in str(item.get("line", ""))
            )
            else "not visible"
        )
        return (
            "AMD was selected by allocator"
            f" with source={row.get('source')} notional={row.get('proposed_notional')}, then dispatch skipped it for "
            f"{row.get('dispatch_skip_reason')}. Dispatch RVOL override metadata was {override_visibility} in the local dispatch evidence; "
            f"news_score={check.get('news_score') if check else None} catalyst_score={check.get('catalyst_score') if check else None} "
            f"effective_min_rel_volume={check.get('effective_min_rel_volume') if check else None} missing_fields={missing}."
        )
    return (
        f"{symbol}: outcome={row.get('final_pipeline_outcome')} route={row.get('route')} "
        f"source={row.get('source')} dispatch_reason={row.get('dispatch_skip_reason')} "
        f"categories={row.get('mismatch_categories')}"
    )


def build_allocator_dispatch_mismatch_report(
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
        latest = latest_allocator_dispatch_mismatch_date(project_root=root, data_dir=data, user_id=user_id)
        day = latest or day
    discovered = _discover_log_paths(root, data_dir=data, day=day, extra_paths=log_paths)
    rows, used = _parse_logs(discovered, day=day)
    _merge_rows(rows, _sqlite_rows(data, day=day, user_id=user_id))
    candidates = [_finalize(dict(row)) for row in rows.values() if _reaches_allocator(row)]
    candidates.sort(key=lambda row: row["symbol"])

    dispatch_skip_reasons = Counter(
        str(row.get("dispatch_skip_reason"))
        for row in candidates
        if row.get("dispatch_skip_reason")
    )
    mismatch_counts = Counter()
    for row in candidates:
        mismatch_counts.update(row.get("mismatch_categories") or [])
    symbols_most_affected = Counter()
    for row in candidates:
        if row.get("mismatch_categories"):
            symbols_most_affected[str(row.get("symbol"))] += len(row.get("mismatch_categories") or [])

    focus = {symbol: next((row for row in candidates if row["symbol"] == symbol), None) for symbol in _FOCUS_SYMBOLS}
    explanations = {symbol: _explain_symbol(symbol, row) for symbol, row in focus.items()}
    dynamic_dispatch_skips = [
        row for row in candidates if row.get("dispatch_skip_reason") == "dynamic_relative_volume" and row.get("dynamic_candidate")
    ]
    dispatch_override_metadata_visible = any(
        any(
            "override" in str(item.get("line", "")).lower()
            for item in row.get("evidence") or []
            if "ALLOCATOR_DISPATCH" in str(item.get("line", ""))
        )
        for row in dynamic_dispatch_skips
    )
    metadata_by_symbol: dict[str, dict[str, Any]] = {}
    for row in dynamic_dispatch_skips:
        check = row.get("dispatch_dynamic_rvol_check") if isinstance(row.get("dispatch_dynamic_rvol_check"), Mapping) else {}
        missing = set(row.get("dispatch_dynamic_missing_fields") or [])
        scanner_effective = _safe_float(check.get("scanner_effective_min_rel_volume")) if check else None
        effective = _safe_float(check.get("effective_min_rel_volume")) if check else None
        rel_volume = _safe_float(check.get("rel_volume")) if check else None
        applied_same_override = None
        if scanner_effective is not None and effective is not None:
            applied_same_override = abs(scanner_effective - effective) <= 1e-9
        metadata_by_symbol[str(row["symbol"])] = {
            "received_news_score": bool(check) and check.get("news_score") is not None,
            "received_catalyst_score": bool(check) and check.get("catalyst_score") is not None,
            "received_effective_min_rel_volume": bool(check) and check.get("effective_min_rel_volume") is not None,
            "scanner_effective_min_rel_volume": scanner_effective,
            "dispatch_effective_min_rel_volume": effective,
            "rel_volume": rel_volume,
            "override_active": check.get("override_active") if check else None,
            "applied_same_rvol_override_as_scanner": applied_same_override,
            "missing_fields": sorted(missing),
        }
    return {
        "report": "allocator_dispatch_mismatch",
        "research_only": True,
        "date": day,
        "user": user_id,
        "source_files": used,
        "summary": {
            "total_entry_eval_passed": sum(1 for row in rows.values() if row.get("entry_eval_final") is True),
            "total_allocator_reached": len(candidates),
            "total_allocator_selected": sum(1 for row in candidates if row.get("allocator_selected")),
            "total_dispatch_started": sum(1 for row in candidates if row.get("dispatch_started")),
            "total_dispatch_skipped": sum(1 for row in candidates if row.get("dispatch_result") == "skipped"),
            "dispatch_skip_reasons": dict(sorted(dispatch_skip_reasons.items())),
            "symbols_most_affected": dict(symbols_most_affected.most_common()),
            "mismatch_counts_by_category": dict(sorted(mismatch_counts.items())),
        },
        "dynamic_rvol_consistency": {
            "dispatch_rechecked_dynamic_relative_volume": bool(dynamic_dispatch_skips),
            "affected_symbols": sorted(str(row["symbol"]) for row in dynamic_dispatch_skips),
            "scanner_or_entry_override_metadata_visible_at_dispatch": dispatch_override_metadata_visible,
            "metadata_by_symbol": metadata_by_symbol,
            "received_news_score_count": sum(1 for row in metadata_by_symbol.values() if row["received_news_score"]),
            "received_catalyst_score_count": sum(1 for row in metadata_by_symbol.values() if row["received_catalyst_score"]),
            "received_effective_min_rel_volume_count": sum(
                1 for row in metadata_by_symbol.values() if row["received_effective_min_rel_volume"]
            ),
            "missing_fields_by_symbol": {
                symbol: row["missing_fields"] for symbol, row in sorted(metadata_by_symbol.items()) if row["missing_fields"]
            },
            "inference": (
                "dispatch is applying a dynamic_relative_volume gate after allocator selection; local logs do not prove "
                "that scanner/news/catalyst RVOL override metadata is available at dispatch"
                if dynamic_dispatch_skips
                else "no allocator-selected dynamic_relative_volume dispatch skip found in parsed evidence"
            ),
        },
        "candidates": candidates,
        "focus_symbols": focus,
        "explanations": explanations,
    }


def render_allocator_dispatch_mismatch_report(report: Mapping[str, Any]) -> str:
    summary = report.get("summary") if isinstance(report.get("summary"), Mapping) else {}
    consistency = report.get("dynamic_rvol_consistency") if isinstance(report.get("dynamic_rvol_consistency"), Mapping) else {}
    lines = [
        f"Allocator Dispatch Mismatch Report - {report.get('date')} user={report.get('user')}",
        "Research-only: no trading behavior, config, allocator, dispatch, scanner, risk model, or thresholds changed.",
        "",
        "Summary",
        f"- source_files: {len(report.get('source_files') or [])}",
        f"- total_entry_eval_passed: {summary.get('total_entry_eval_passed', 0)}",
        f"- total_allocator_reached: {summary.get('total_allocator_reached', 0)}",
        f"- total_allocator_selected: {summary.get('total_allocator_selected', 0)}",
        f"- total_dispatch_started: {summary.get('total_dispatch_started', 0)}",
        f"- total_dispatch_skipped: {summary.get('total_dispatch_skipped', 0)}",
        f"- dispatch_skip_reasons: {summary.get('dispatch_skip_reasons')}",
        f"- mismatch_counts_by_category: {summary.get('mismatch_counts_by_category')}",
        f"- symbols_most_affected: {summary.get('symbols_most_affected')}",
        "",
        "Dynamic RVOL Consistency",
        f"- dispatch_rechecked_dynamic_relative_volume: {consistency.get('dispatch_rechecked_dynamic_relative_volume')}",
        f"- affected_symbols: {', '.join(consistency.get('affected_symbols') or []) or 'none'}",
        f"- scanner_or_entry_override_metadata_visible_at_dispatch: {consistency.get('scanner_or_entry_override_metadata_visible_at_dispatch')}",
        f"- received_news_score_count: {consistency.get('received_news_score_count', 0)}",
        f"- received_catalyst_score_count: {consistency.get('received_catalyst_score_count', 0)}",
        f"- received_effective_min_rel_volume_count: {consistency.get('received_effective_min_rel_volume_count', 0)}",
        f"- missing_fields_by_symbol: {consistency.get('missing_fields_by_symbol')}",
        f"- inference: {consistency.get('inference')}",
        "",
        "Dispatch Metadata by Symbol",
    ]
    for symbol, block in sorted((consistency.get("metadata_by_symbol") or {}).items()):
        lines.append(
            "- {symbol}: news={received_news_score} catalyst={received_catalyst_score} "
            "effective_min={received_effective_min_rel_volume} scanner_min={scanner_effective_min_rel_volume} "
            "dispatch_min={dispatch_effective_min_rel_volume} override_active={override_active} "
            "same_override={applied_same_rvol_override_as_scanner} missing={missing_fields}".format(
                symbol=symbol,
                **block,
            )
        )
    lines.extend(
        [
        "",
        "Focus Symbols",
        ]
    )
    explanations = report.get("explanations") if isinstance(report.get("explanations"), Mapping) else {}
    for symbol in _FOCUS_SYMBOLS:
        lines.append(f"- {explanations.get(symbol)}")
    lines.extend(["", "Allocator Candidates"])
    candidates = report.get("candidates") if isinstance(report.get("candidates"), list) else []
    if not candidates:
        lines.append("- none")
    for row in candidates[:150]:
        lines.append(
            "- {symbol} route={route} dynamic={dynamic_candidate} source={source} entry_final={entry_eval_final} "
            "ranked={allocator_ranked} selected={allocator_selected} notional={proposed_notional} trade_size={trade_size} "
            "min_cash={minimum_cash_to_deploy} gross_headroom={gross_headroom} dispatch={dispatch_result} "
            "dispatch_reason={dispatch_skip_reason} dispatch_rvol={dispatch_dynamic_rvol_check} "
            "missing={dispatch_dynamic_missing_fields} outcome={final_pipeline_outcome} categories={mismatch_categories}".format(**row)
        )
    return "\n".join(lines) + "\n"


def write_allocator_dispatch_mismatch_report(
    *,
    project_root: Path | str = ".",
    data_dir: Path | str = "data",
    day: str,
    user_id: str = "paper_bot",
    log_paths: Sequence[Path | str] | None = None,
) -> tuple[Path, Path, dict[str, Any]]:
    data = Path(data_dir)
    report = build_allocator_dispatch_mismatch_report(
        project_root=project_root,
        data_dir=data,
        day=day,
        user_id=user_id,
        log_paths=log_paths,
    )
    out_dir = data / "research" / "allocator_dispatch_mismatch"
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{report['date']}_{_safe_user(user_id)}"
    json_path = out_dir / f"{stem}.json"
    text_path = out_dir / f"{stem}.txt"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    text_path.write_text(render_allocator_dispatch_mismatch_report(report), encoding="utf-8")
    return json_path, text_path, report
