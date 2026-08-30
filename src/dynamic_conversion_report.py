"""Research-only dynamic candidate conversion funnel report."""

from __future__ import annotations

import gzip
import json
import math
import re
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")
_KV_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=([^ ]+)")
_SYMBOL_RE = re.compile(r"\bsymbol=(?P<symbol>[A-Z][A-Z0-9.\-]{0,9})\b")
_ENTRY_EVAL_RE = re.compile(
    r"\b(?P<symbol>[A-Z][A-Z0-9.\-]{0,9})\s+ENTRY_EVAL\s+"
    r"route=(?P<route>[^ ]+).*?\bfinal=(?P<final>[TF]|true|false|True|False)\b.*?"
    r"\breason=(?P<reason>.+?)(?=\s+[A-Za-z_][A-Za-z0-9_]*=|$)"
)
_ISO_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
_COMPACT_DATE_RE = re.compile(r"(\d{8})")
_LIST_SYMBOL_RE = re.compile(r"[A-Z][A-Z0-9.\-]{0,9}")
_MONTHS = {
    "Jan": 1,
    "Feb": 2,
    "Mar": 3,
    "Apr": 4,
    "May": 5,
    "Jun": 6,
    "Jul": 7,
    "Aug": 8,
    "Sep": 9,
    "Oct": 10,
    "Nov": 11,
    "Dec": 12,
}
_FOCUS_SYMBOLS = ("ASTN", "RKLZ", "CIIT", "DSY", "AVGO", "GOOGL")
_STAGE_ORDER = {
    "DYNAMIC_ACCEPTED": 1,
    "DYNAMIC_SELECTED": 2,
    "DYNAMIC_UNIVERSE_ADDED": 3,
    "ENTRY_EVAL": 4,
    "ENTRY_EVAL_PASS": 5,
    "ENTRY_TO_ALLOCATOR_TRACE": 6,
    "ALLOCATOR_INPUT": 7,
    "ALLOCATOR_DECISION": 8,
    "ORDER_INTENT": 9,
    "BUY_SUBMITTED": 10,
}


@dataclass(frozen=True)
class DynamicConversionPaths:
    json_path: Path
    text_path: Path


@dataclass
class DynamicSymbolState:
    symbol: str
    first_accepted_time: str | None = None
    accepted_count: int = 0
    selection_count: int = 0
    dynamic_score: float | None = None
    appeared_in_dynamic_universe: bool = False
    reached_entry_eval: bool = False
    entry_eval_route: str | None = None
    entry_eval_final: bool | None = None
    entry_eval_reject_reason: str | None = None
    reached_allocator_trace: bool = False
    reached_allocator: bool = False
    allocator_produced_action: bool = False
    order_intent_produced: bool = False
    bought_or_submitted: bool = False
    selected_entry_trace: dict[str, Any] | None = None
    selected_entry_skip_reason: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)

    def note(
        self,
        stage: str,
        *,
        timestamp: str | None = None,
        reason: str | None = None,
        route: str | None = None,
        score: Any = None,
        source: str | None = None,
        line_number: int | None = None,
    ) -> None:
        score_float = _safe_float(score)
        if score_float is not None:
            self.dynamic_score = score_float
        self.events.append(
            {
                "stage": stage,
                "timestamp": timestamp,
                "reason": reason,
                "route": route,
                "score": score_float,
                "source": source,
                "line_number": line_number,
            }
        )


def _safe_user(user_id: str) -> str:
    return "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in str(user_id or "default")) or "default"


def _safe_float(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return out if math.isfinite(out) else None


def _parse_kv(line: str) -> dict[str, str]:
    return {match.group(1): match.group(2).rstrip(",;") for match in _KV_RE.finditer(line)}


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


def _iso_timestamp(value: Any) -> str | None:
    dt = _parse_timestamp(value)
    return dt.astimezone(_ET).isoformat() if dt is not None else None


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


def _line_timestamp(line: str, *, day: str) -> str | None:
    iso = re.search(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?)", line)
    if iso:
        return _iso_timestamp(iso.group(1))
    match = re.match(r"^(?P<mon>[A-Z][a-z]{2})\s+(?P<day>\d{1,2})\s+(?P<time>\d{2}:\d{2}:\d{2})\b", line)
    if not match:
        return None
    expected = datetime.strptime(day, "%Y-%m-%d").date()
    month = _MONTHS.get(match.group("mon"))
    if month != expected.month or int(match.group("day")) != expected.day:
        return None
    hh, mm, ss = (int(part) for part in match.group("time").split(":"))
    return datetime(expected.year, expected.month, expected.day, hh, mm, ss, tzinfo=_ET).isoformat()


def _read_text(path: Path) -> str:
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    return path.read_text(encoding="utf-8", errors="replace")


def _json_load(path: Path) -> Mapping[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, Mapping) else None


def _json_loads_maybe(value: Any) -> Any:
    if value is None or isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except Exception:
        return None


def _state(states: dict[str, DynamicSymbolState], symbol: str) -> DynamicSymbolState:
    sym = str(symbol or "").strip().upper()
    if sym not in states:
        states[sym] = DynamicSymbolState(symbol=sym)
    return states[sym]


def _candidate_symbol(raw: Any) -> str | None:
    if isinstance(raw, Mapping):
        sym = str(raw.get("symbol") or "").strip().upper()
    else:
        sym = str(raw or "").strip().upper()
    return sym or None


def _candidate_score(raw: Any) -> Any:
    return raw.get("score") if isinstance(raw, Mapping) else None


def _selected_symbols(payload: Mapping[str, Any]) -> set[str]:
    selected = payload.get("selected") or payload.get("selected_symbols") or []
    out: set[str] = set()
    if isinstance(selected, list):
        for raw in selected:
            sym = _candidate_symbol(raw)
            if sym:
                out.add(sym)
    return out


def _iter_dynamic_scan_paths(data_dir: Path, *, day: str, user_id: str) -> list[Path]:
    roots = [data_dir / "dynamic_scan_history", data_dir / "dynamic_scan_history" / "daily"]
    safe_user = _safe_user(user_id)
    paths: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        for path in root.glob("*.json"):
            if _date_from_path(path) != day:
                continue
            if path.name.endswith(f"_{safe_user}.json") or path.name.endswith("_default.json"):
                paths.append(path)
    return sorted(dict.fromkeys(paths))


def _ingest_dynamic_scan_history(
    states: dict[str, DynamicSymbolState],
    *,
    data_dir: Path,
    day: str,
    user_id: str,
) -> list[str]:
    used: list[str] = []
    for path in _iter_dynamic_scan_paths(data_dir, day=day, user_id=user_id):
        payload = _json_load(path)
        if payload is None:
            continue
        used.append(str(path))
        generated_at = _iso_timestamp(payload.get("generated_at")) or _date_from_path(path)
        selected_set = _selected_symbols(payload)
        for raw in payload.get("accepted") or []:
            if not isinstance(raw, Mapping):
                continue
            sym = _candidate_symbol(raw)
            if not sym:
                continue
            st = _state(states, sym)
            st.accepted_count += 1
            if st.first_accepted_time is None:
                st.first_accepted_time = _iso_timestamp(raw.get("timestamp")) or generated_at
            st.note("DYNAMIC_ACCEPTED", timestamp=st.first_accepted_time, score=raw.get("score"), source=str(path))
        for raw in payload.get("candidates") or []:
            if not isinstance(raw, Mapping):
                continue
            sym = _candidate_symbol(raw)
            if not sym:
                continue
            accepted = raw.get("accepted")
            if accepted is True or str(accepted).lower() == "true":
                st = _state(states, sym)
                st.accepted_count += 1
                ts = _iso_timestamp(raw.get("timestamp")) or generated_at
                if st.first_accepted_time is None:
                    st.first_accepted_time = ts
                st.note("DYNAMIC_ACCEPTED", timestamp=ts, score=raw.get("score"), source=str(path))
                if bool(raw.get("selected")):
                    selected_set.add(sym)
        for raw in payload.get("selected") or []:
            sym = _candidate_symbol(raw)
            if not sym:
                continue
            selected_set.add(sym)
            st = _state(states, sym)
            st.selection_count += 1
            st.note("DYNAMIC_SELECTED", timestamp=generated_at, score=_candidate_score(raw), source=str(path))
        for sym in selected_set:
            st = _state(states, sym)
            if st.selection_count == 0:
                st.selection_count += 1
                st.note("DYNAMIC_SELECTED", timestamp=generated_at, source=str(path))
    return used


def _discover_log_paths(project_root: Path, data_dir: Path, *, day: str, extra_paths: Sequence[Path | str] | None) -> list[Path]:
    candidates: list[Path] = []
    preferred = data_dir / "review" / day / "paper_full.log"
    if preferred.exists():
        candidates.append(preferred)
    for root in (
        data_dir / "review" / day,
        data_dir / "logs",
        data_dir / "debug_logs",
        project_root / "reports" / "debug",
    ):
        if not root.exists():
            continue
        globber = root.rglob if root.name == "debug_logs" else root.glob
        for pattern in ("*.log", "*.txt", "*.log.gz"):
            for path in globber(pattern):
                if path.is_file() and (_date_from_path(path) in {None, day} or day in path.name):
                    candidates.append(path)
    for raw in extra_paths or []:
        path = Path(raw)
        if path.exists() and path.is_file():
            candidates.append(path)
    return sorted(dict.fromkeys(candidates))


def _symbols_from_list_text(text: str) -> list[str]:
    return [sym.upper() for sym in _LIST_SYMBOL_RE.findall(text)]


def _extract_universe_added(line: str) -> list[str]:
    match = re.search(r"(?:added|fastlane)=\[(?P<symbols>[^\]]*)\]", line)
    if match:
        return _symbols_from_list_text(match.group("symbols"))
    match = re.search(r"(?:added|fastlane)=(?P<symbols>[A-Z0-9.,_\-]+)", line)
    if match:
        return _symbols_from_list_text(match.group("symbols"))
    return []


def _symbol_from_line(line: str, kv: Mapping[str, str]) -> str | None:
    sym = str(kv.get("symbol") or "").strip().upper()
    if sym:
        return sym
    match = _SYMBOL_RE.search(line)
    if match:
        return match.group("symbol").upper()
    entry_match = _ENTRY_EVAL_RE.search(line)
    if entry_match:
        return entry_match.group("symbol").upper()
    skip = re.search(r"\bSKIP\s+(?P<symbol>[A-Z][A-Z0-9.\-]{0,9}):", line)
    if skip:
        return skip.group("symbol").upper()
    return None


def _reason_from_line(line: str, kv: Mapping[str, str]) -> str | None:
    if kv.get("reason"):
        return kv.get("reason")
    match = re.search(r"reason=(?P<reason>.+?)(?=\s+[A-Za-z_][A-Za-z0-9_]*=|$)", line)
    if match:
        return match.group("reason").strip()
    return None


def _ingest_log_line(
    states: dict[str, DynamicSymbolState],
    *,
    line: str,
    day: str,
    source: str,
    line_number: int,
) -> None:
    kv = _parse_kv(line)
    ts = _line_timestamp(line, day=day)
    if "DYNAMIC_ACCEPTED" in line:
        sym = _symbol_from_line(line, kv)
        if sym:
            st = _state(states, sym)
            st.accepted_count += 1
            st.first_accepted_time = st.first_accepted_time or ts
            st.note("DYNAMIC_ACCEPTED", timestamp=ts, score=kv.get("score"), source=source, line_number=line_number)
    if "DYNAMIC_SELECTED" in line and "DYNAMIC_SELECTED_ENTRY" not in line:
        sym = _symbol_from_line(line, kv)
        if sym:
            st = _state(states, sym)
            st.selection_count += 1
            st.note("DYNAMIC_SELECTED", timestamp=ts, score=kv.get("score"), source=source, line_number=line_number)
    if "DYNAMIC_UNIVERSE" in line:
        for sym in _extract_universe_added(line):
            st = _state(states, sym)
            st.appeared_in_dynamic_universe = True
            st.note("DYNAMIC_UNIVERSE_ADDED", timestamp=ts, source=source, line_number=line_number)
    if "DYNAMIC_SELECTED_ENTRY_TRACE" in line:
        sym = _symbol_from_line(line, kv)
        if sym:
            st = _state(states, sym)
            st.selected_entry_trace = {
                "in_universe": kv.get("in_universe"),
                "will_evaluate": kv.get("will_evaluate"),
                "reason": _reason_from_line(line, kv),
            }
            st.note(
                "DYNAMIC_SELECTED_ENTRY_TRACE",
                timestamp=ts,
                reason=st.selected_entry_trace.get("reason"),
                source=source,
                line_number=line_number,
            )
    if "DYNAMIC_SELECTED_ENTRY_SKIPPED" in line or "DYNAMIC_SELECTED_DROPPED" in line:
        sym = _symbol_from_line(line, kv)
        if sym:
            st = _state(states, sym)
            st.selected_entry_skip_reason = _reason_from_line(line, kv)
            st.note(
                "DYNAMIC_SELECTED_ENTRY_SKIPPED",
                timestamp=ts,
                reason=st.selected_entry_skip_reason,
                source=source,
                line_number=line_number,
            )
    entry_match = _ENTRY_EVAL_RE.search(line)
    if entry_match:
        sym = entry_match.group("symbol").upper()
        final = entry_match.group("final").strip().lower() in {"t", "true", "1"}
        st = _state(states, sym)
        st.reached_entry_eval = True
        st.entry_eval_route = entry_match.group("route")
        st.entry_eval_final = final
        st.entry_eval_reject_reason = None if final else entry_match.group("reason").strip()
        st.note(
            "ENTRY_EVAL_PASS" if final else "ENTRY_EVAL",
            timestamp=ts,
            route=st.entry_eval_route,
            reason=st.entry_eval_reject_reason,
            source=source,
            line_number=line_number,
        )
    if "ENTRY_TO_ALLOCATOR_TRACE" in line:
        sym = _symbol_from_line(line, kv)
        if sym:
            st = _state(states, sym)
            st.reached_allocator_trace = True
            st.note("ENTRY_TO_ALLOCATOR_TRACE", timestamp=ts, route=kv.get("route"), source=source, line_number=line_number)
    if "ALLOCATOR_INPUT_SYMBOLS" in line or "ALLOCATOR_INPUT_DETAIL" in line:
        symbols = []
        match = re.search(r"symbols=(?P<symbols>[^ ]+)", line)
        if match:
            symbols = _symbols_from_list_text(match.group("symbols"))
        for sym in symbols:
            st = _state(states, sym)
            st.reached_allocator = True
            st.note("ALLOCATOR_INPUT", timestamp=ts, source=source, line_number=line_number)
    allocator_markers = (
        "ALLOCATOR_NO_ACTION_DETAIL",
        "ALLOCATOR_SKIP_REASON",
        "ALLOCATOR_REJECT_REASON",
        "ALLOCATOR_FILTER_REJECT",
        "ALLOCATOR_DYNAMIC_SELECTED",
    )
    if any(marker in line for marker in allocator_markers):
        sym = _symbol_from_line(line, kv)
        if sym:
            st = _state(states, sym)
            st.reached_allocator = True
            st.note("ALLOCATOR_DECISION", timestamp=ts, reason=_reason_from_line(line, kv), source=source, line_number=line_number)
    if "ALLOCATOR_ACTION_CREATED" in line:
        sym = _symbol_from_line(line, kv)
        if sym:
            st = _state(states, sym)
            st.reached_allocator = True
            st.allocator_produced_action = True
            st.note("ALLOCATOR_DECISION", timestamp=ts, source=source, line_number=line_number)
    if "ORDER_INTENT" in line or "ALLOCATOR_ORDER_INTENT" in line or "OPTION_ORDER_INTENT" in line:
        sym = _symbol_from_line(line, kv)
        if sym:
            st = _state(states, sym)
            st.order_intent_produced = True
            st.note("ORDER_INTENT", timestamp=ts, source=source, line_number=line_number)
    if "ALLOCATOR_ACTION_SUBMITTED" in line or "ORDER_SUBMITTED" in line or re.search(r"\bBUY\s+[A-Z][A-Z0-9.\-]{0,9}\b", line):
        sym = _symbol_from_line(line, kv)
        if sym:
            st = _state(states, sym)
            st.bought_or_submitted = True
            st.note("BUY_SUBMITTED", timestamp=ts, source=source, line_number=line_number)


def _ingest_logs(
    states: dict[str, DynamicSymbolState],
    *,
    paths: Sequence[Path],
    day: str,
) -> list[str]:
    used: list[str] = []
    markers = (
        "DYNAMIC_ACCEPTED",
        "DYNAMIC_SELECTED",
        "DYNAMIC_UNIVERSE",
        "ENTRY_EVAL",
        "ENTRY_TO_ALLOCATOR_TRACE",
        "ALLOCATOR_",
        "ORDER_INTENT",
        "ORDER_SUBMITTED",
        "BUY ",
    )
    for path in paths:
        try:
            lines = _read_text(path).splitlines()
        except OSError:
            continue
        useful = False
        for index, line in enumerate(lines, start=1):
            if not any(marker in line for marker in markers):
                continue
            _ingest_log_line(states, line=line, day=day, source=str(path), line_number=index)
            useful = True
        if useful:
            used.append(str(path))
    return used


def _sqlite_tables(conn: sqlite3.Connection) -> set[str]:
    try:
        return {str(row[0]) for row in conn.execute("select name from sqlite_master where type='table'")}
    except sqlite3.Error:
        return set()


def _ingest_sqlite(
    states: dict[str, DynamicSymbolState],
    *,
    data_dir: Path,
    day: str,
    user_id: str,
) -> str | None:
    db_path = data_dir / "algo_live.db"
    if not db_path.exists():
        return None
    try:
        conn = sqlite3.connect(db_path)
    except sqlite3.Error:
        return None
    try:
        tables = _sqlite_tables(conn)
        day_prefix = f"{day}%"
        args = (user_id, day_prefix)
        if "dynamic_scans" in tables:
            try:
                rows = conn.execute(
                    "select ts, selected_json, candidates_json, payload_json from dynamic_scans where user_id=? and ts like ? order by ts",
                    args,
                )
                for ts, selected_json, candidates_json, payload_json in rows:
                    selected = _json_loads_maybe(selected_json) or []
                    selected_set = {str(sym).strip().upper() for sym in selected if str(sym).strip()}
                    candidates = _json_loads_maybe(candidates_json)
                    if candidates is None:
                        payload = _json_loads_maybe(payload_json)
                        candidates = payload.get("candidates") if isinstance(payload, Mapping) else []
                    for raw in candidates or []:
                        if not isinstance(raw, Mapping):
                            continue
                        sym = _candidate_symbol(raw)
                        if not sym:
                            continue
                        accepted = raw.get("accepted")
                        if accepted is True or str(accepted).lower() == "true":
                            st = _state(states, sym)
                            st.accepted_count += 1
                            st.first_accepted_time = st.first_accepted_time or _iso_timestamp(raw.get("timestamp")) or _iso_timestamp(ts)
                            st.note("DYNAMIC_ACCEPTED", timestamp=_iso_timestamp(ts), score=raw.get("score"), source=str(db_path))
                        if sym in selected_set or bool(raw.get("selected")):
                            st = _state(states, sym)
                            st.selection_count += 1
                            st.note("DYNAMIC_SELECTED", timestamp=_iso_timestamp(ts), score=raw.get("score"), source=str(db_path))
                    for sym in selected_set:
                        st = _state(states, sym)
                        if st.selection_count == 0:
                            st.selection_count += 1
                        st.note("DYNAMIC_SELECTED", timestamp=_iso_timestamp(ts), source=str(db_path))
            except sqlite3.Error:
                pass
        if "entry_evaluations" in tables:
            try:
                rows = conn.execute(
                    "select ts, symbol, route, final, reason from entry_evaluations where user_id=? and ts like ? order by ts",
                    args,
                )
                for ts, symbol, route, final, reason in rows:
                    sym = str(symbol or "").upper()
                    if not sym:
                        continue
                    st = _state(states, sym)
                    st.reached_entry_eval = True
                    st.entry_eval_route = route
                    st.entry_eval_final = bool(final)
                    st.entry_eval_reject_reason = None if bool(final) else str(reason or "unknown")
                    st.note(
                        "ENTRY_EVAL_PASS" if bool(final) else "ENTRY_EVAL",
                        timestamp=_iso_timestamp(ts),
                        route=route,
                        reason=st.entry_eval_reject_reason,
                        source=str(db_path),
                    )
            except sqlite3.Error:
                pass
        if "entry_terminal_outcomes" in tables:
            try:
                rows = conn.execute(
                    "select ts, symbol, route, stage, reason from entry_terminal_outcomes where user_id=? and ts like ? order by ts",
                    args,
                )
                for ts, symbol, route, stage, reason in rows:
                    sym = str(symbol or "").upper()
                    if not sym:
                        continue
                    st = _state(states, sym)
                    stage_s = str(stage or "")
                    if "allocator" in stage_s:
                        st.reached_allocator = True
                    if stage_s in {"allocator_action_created", "allocator_order_intent"}:
                        st.allocator_produced_action = True
                    if stage_s in {"allocator_order_intent", "order_intent"}:
                        st.order_intent_produced = True
                    if stage_s in {"submitted", "broker_submitted"}:
                        st.bought_or_submitted = True
                    st.note("ALLOCATOR_DECISION", timestamp=_iso_timestamp(ts), route=route, reason=str(reason or stage_s), source=str(db_path))
            except sqlite3.Error:
                pass
        if "trades" in tables:
            try:
                rows = conn.execute("select ts, symbol, side, status from trades where user_id=? and ts like ? order by ts", args)
                for ts, symbol, side, status in rows:
                    sym = str(symbol or "").upper()
                    if not sym:
                        continue
                    st = _state(states, sym)
                    if str(side or "").lower() == "buy":
                        st.bought_or_submitted = True
                        st.note("BUY_SUBMITTED", timestamp=_iso_timestamp(ts), reason=str(status or ""), source=str(db_path))
            except sqlite3.Error:
                pass
    finally:
        conn.close()
    return str(db_path)


def _final_stage(st: DynamicSymbolState) -> str:
    best = "not_observed"
    for event in st.events:
        stage = str(event.get("stage") or "")
        if _STAGE_ORDER.get(stage, 0) > _STAGE_ORDER.get(best, 0):
            best = stage
    return best


def _drop_reason(st: DynamicSymbolState) -> str:
    final = _final_stage(st)
    if st.bought_or_submitted:
        return "order_submitted_or_bought"
    if st.order_intent_produced:
        return "order_intent_observed_no_submit"
    if st.allocator_produced_action:
        return "allocator_action_observed_no_order_intent"
    if st.reached_allocator:
        reasons = [str(event.get("reason") or "") for event in st.events if event.get("reason")]
        return next((reason for reason in reversed(reasons) if reason), "allocator_no_order_action_observed")
    if st.reached_allocator_trace:
        return "entry_trace_reached_allocator_but_no_allocator_decision_observed"
    if st.entry_eval_final is False:
        return st.entry_eval_reject_reason or "entry_eval_failed"
    if st.entry_eval_final is True:
        return "entry_eval_passed_but_no_allocator_trace_observed"
    if st.reached_entry_eval:
        return "entry_eval_seen_without_final_result"
    if st.selected_entry_skip_reason:
        return st.selected_entry_skip_reason
    if st.selected_entry_trace:
        trace = st.selected_entry_trace
        if str(trace.get("will_evaluate")).lower() == "false":
            return str(trace.get("reason") or "selected_entry_trace_will_not_evaluate")
        if str(trace.get("in_universe")).lower() == "false":
            return "selected_dynamic_not_in_universe"
    if st.appeared_in_dynamic_universe:
        return "dynamic_universe_added_but_no_entry_eval_observed"
    if st.selection_count > 0:
        return "selected_but_not_seen_in_dynamic_universe_or_entry_eval"
    if st.accepted_count > 0:
        return "accepted_but_not_selected"
    return f"not_observed_after_{final}"


def _row(st: DynamicSymbolState) -> dict[str, Any]:
    final = _final_stage(st)
    return {
        "symbol": st.symbol,
        "first_accepted_time": st.first_accepted_time,
        "accepted_count": st.accepted_count,
        "selection_count": st.selection_count,
        "dynamic_score": st.dynamic_score,
        "appeared_in_dynamic_universe": st.appeared_in_dynamic_universe,
        "reached_entry_eval": st.reached_entry_eval,
        "entry_eval_route": st.entry_eval_route,
        "entry_eval_final": st.entry_eval_final,
        "entry_eval_reject_reason": st.entry_eval_reject_reason,
        "reached_allocator_trace": st.reached_allocator_trace,
        "reached_allocator": st.reached_allocator,
        "allocator_produced_action": st.allocator_produced_action,
        "order_intent_produced": st.order_intent_produced,
        "bought_or_submitted": st.bought_or_submitted,
        "final_observed_pipeline_stage": final,
        "inferred_drop_reason": _drop_reason(st),
        "events": st.events,
    }


def latest_dynamic_conversion_date(*, data_dir: Path | str = "data", user_id: str = "paper_bot") -> str | None:
    data = Path(data_dir)
    safe_user = _safe_user(user_id)
    dates: set[str] = set()
    for root in (data / "dynamic_scan_history", data / "review"):
        if not root.exists():
            continue
        for path in root.rglob("*.json") if root.name == "dynamic_scan_history" else root.rglob("*"):
            day = _date_from_path(path)
            if not day:
                continue
            if root.name == "dynamic_scan_history" and not (
                path.name.endswith(f"_{safe_user}.json") or path.name.endswith("_default.json")
            ):
                continue
            dates.add(day)
    return sorted(dates)[-1] if dates else None


def build_dynamic_conversion_report(
    *,
    project_root: Path | str = ".",
    data_dir: Path | str = "data",
    day: str | date,
    user_id: str = "paper_bot",
    log_paths: Sequence[Path | str] | None = None,
    focus_symbols: Sequence[str] = _FOCUS_SYMBOLS,
) -> dict[str, Any]:
    """Build a read-only dynamic conversion funnel report."""
    data = Path(data_dir)
    root = Path(project_root)
    day_s = (
        latest_dynamic_conversion_date(data_dir=data, user_id=user_id)
        if str(day).lower() == "latest"
        else day.isoformat()
        if isinstance(day, date)
        else str(day)
    )
    if not day_s:
        raise FileNotFoundError("No dynamic conversion date found.")
    states: dict[str, DynamicSymbolState] = {}
    scan_files = _ingest_dynamic_scan_history(states, data_dir=data, day=day_s, user_id=user_id)
    discovered_logs = _discover_log_paths(root, data, day=day_s, extra_paths=log_paths)
    used_logs = _ingest_logs(states, paths=discovered_logs, day=day_s)
    sqlite_path = _ingest_sqlite(states, data_dir=data, day=day_s, user_id=user_id)
    for sym in focus_symbols:
        _state(states, sym.upper())
    rows = sorted(
        [_row(st) for st in states.values() if st.accepted_count > 0 or st.selection_count > 0 or st.symbol in set(focus_symbols)],
        key=lambda row: (row["symbol"] not in set(focus_symbols), row["symbol"]),
    )
    accepted_or_selected = [row for row in rows if int(row.get("accepted_count") or 0) > 0 or int(row.get("selection_count") or 0) > 0]
    drop_counts = Counter(str(row.get("final_observed_pipeline_stage") or "not_observed") for row in accepted_or_selected)
    summary = {
        "total_dynamic_accepted": len([row for row in rows if int(row.get("accepted_count") or 0) > 0]),
        "total_selected": len([row for row in rows if int(row.get("selection_count") or 0) > 0]),
        "reached_entry_eval": len([row for row in accepted_or_selected if row.get("reached_entry_eval")]),
        "entry_eval_passed": len([row for row in accepted_or_selected if row.get("entry_eval_final") is True]),
        "reached_allocator": len([row for row in accepted_or_selected if row.get("reached_allocator") or row.get("reached_allocator_trace")]),
        "produced_order_intent": len([row for row in accepted_or_selected if row.get("order_intent_produced")]),
        "bought_or_submitted": len([row for row in accepted_or_selected if row.get("bought_or_submitted")]),
        "drop_counts_by_stage": dict(sorted(drop_counts.items())),
    }
    answers = {
        "did_accepted_dynamic_candidates_reach_entry_eval": summary["reached_entry_eval"] > 0,
        "accepted_reached_entry_eval_count": summary["reached_entry_eval"],
        "accepted_or_selected_count": len(accepted_or_selected),
        "silent_loss_between_scanner_and_entry_loop": any(
            row.get("final_observed_pipeline_stage") in {"DYNAMIC_ACCEPTED", "DYNAMIC_SELECTED", "DYNAMIC_UNIVERSE_ADDED"}
            for row in accepted_or_selected
        ),
        "most_common_drop_stage": drop_counts.most_common(1)[0][0] if drop_counts else None,
    }
    return {
        "report": "dynamic_conversion",
        "research_only": True,
        "date": day_s,
        "user_id": user_id,
        "source_files": {
            "dynamic_scan_history": scan_files,
            "logs": used_logs,
            "sqlite_event_store": sqlite_path,
        },
        "summary": summary,
        "answers": answers,
        "symbols": rows,
        "focus_symbols": {row["symbol"]: row for row in rows if row["symbol"] in set(focus_symbols)},
    }


def render_dynamic_conversion_report(report: Mapping[str, Any]) -> str:
    """Render a concise operator-facing text report."""
    summary = report.get("summary") if isinstance(report.get("summary"), Mapping) else {}
    answers = report.get("answers") if isinstance(report.get("answers"), Mapping) else {}
    lines = [
        f"Dynamic Conversion Report - {report.get('date')} user={report.get('user_id')}",
        "Research-only: no trading behavior, config, scanner thresholds, allocator behavior, or risk model changed.",
        "",
        "Summary",
        f"- total_dynamic_accepted: {summary.get('total_dynamic_accepted', 0)}",
        f"- total_selected: {summary.get('total_selected', 0)}",
        f"- reached_entry_eval: {summary.get('reached_entry_eval', 0)}",
        f"- entry_eval_passed: {summary.get('entry_eval_passed', 0)}",
        f"- reached_allocator: {summary.get('reached_allocator', 0)}",
        f"- produced_order_intent: {summary.get('produced_order_intent', 0)}",
        f"- bought_or_submitted: {summary.get('bought_or_submitted', 0)}",
        f"- drop_counts_by_stage: {summary.get('drop_counts_by_stage', {})}",
        "",
        "Direct Answers",
        f"- Did accepted dynamic candidates reach entry evaluation? {answers.get('did_accepted_dynamic_candidates_reach_entry_eval')}",
        f"- accepted_or_selected_count: {answers.get('accepted_or_selected_count', 0)}",
        f"- accepted_reached_entry_eval_count: {answers.get('accepted_reached_entry_eval_count', 0)}",
        f"- Are candidates silently lost between scanner and entry loop? {answers.get('silent_loss_between_scanner_and_entry_loop')}",
        f"- most_common_drop_stage: {answers.get('most_common_drop_stage')}",
        "",
        "Focus Symbols",
    ]
    focus = report.get("focus_symbols") if isinstance(report.get("focus_symbols"), Mapping) else {}
    for symbol in _FOCUS_SYMBOLS:
        row = focus.get(symbol) if isinstance(focus.get(symbol), Mapping) else {}
        lines.append(
            "- {symbol}: accepted={accepted} selected={selected} universe={universe} "
            "entry_eval={entry} final={final} route={route} allocator={allocator} "
            "order_intent={intent} bought={bought} final_stage={stage} reason={reason}".format(
                symbol=symbol,
                accepted=row.get("accepted_count", 0),
                selected=row.get("selection_count", 0),
                universe=row.get("appeared_in_dynamic_universe", False),
                entry=row.get("reached_entry_eval", False),
                final=row.get("entry_eval_final"),
                route=row.get("entry_eval_route"),
                allocator=row.get("reached_allocator") or row.get("reached_allocator_trace"),
                intent=row.get("order_intent_produced", False),
                bought=row.get("bought_or_submitted", False),
                stage=row.get("final_observed_pipeline_stage", "not_observed"),
                reason=row.get("inferred_drop_reason"),
            )
        )
    lines.append("")
    lines.append("All Accepted/Selected Dynamic Symbols")
    symbols = report.get("symbols") if isinstance(report.get("symbols"), list) else []
    emitted = 0
    for row in symbols:
        if not isinstance(row, Mapping):
            continue
        if int(row.get("accepted_count") or 0) == 0 and int(row.get("selection_count") or 0) == 0:
            continue
        emitted += 1
        lines.append(
            "- {symbol}: selected={selected} score={score} final_stage={stage} reason={reason}".format(
                symbol=row.get("symbol"),
                selected=row.get("selection_count"),
                score=row.get("dynamic_score"),
                stage=row.get("final_observed_pipeline_stage"),
                reason=row.get("inferred_drop_reason"),
            )
        )
    if emitted == 0:
        lines.append("- none observed")
    return "\n".join(lines) + "\n"


def dynamic_conversion_paths(*, data_dir: Path | str, user_id: str, day: str | date) -> DynamicConversionPaths:
    day_s = day.isoformat() if isinstance(day, date) else str(day)
    root = Path(data_dir) / "research" / "dynamic_conversion"
    stem = f"{day_s}_{_safe_user(user_id)}"
    return DynamicConversionPaths(json_path=root / f"{stem}.json", text_path=root / f"{stem}.txt")


def write_dynamic_conversion_report(
    *,
    project_root: Path | str = ".",
    data_dir: Path | str = "data",
    day: str | date,
    user_id: str = "paper_bot",
    log_paths: Sequence[Path | str] | None = None,
) -> tuple[Path, Path, dict[str, Any]]:
    report = build_dynamic_conversion_report(
        project_root=project_root,
        data_dir=data_dir,
        day=day,
        user_id=user_id,
        log_paths=log_paths,
    )
    paths = dynamic_conversion_paths(data_dir=data_dir, user_id=user_id, day=str(report["date"]))
    paths.json_path.parent.mkdir(parents=True, exist_ok=True)
    paths.json_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    paths.text_path.write_text(render_dynamic_conversion_report(report), encoding="utf-8")
    return paths.json_path, paths.text_path, report


__all__ = [
    "build_dynamic_conversion_report",
    "render_dynamic_conversion_report",
    "write_dynamic_conversion_report",
    "latest_dynamic_conversion_date",
    "dynamic_conversion_paths",
]
