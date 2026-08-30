"""Dispatcher-stage explainability for dynamic allocator actions."""

from __future__ import annotations

import ast
import csv
import gzip
import json
import math
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

_ISO_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
_COMPACT_DATE_RE = re.compile(r"(\d{8})")
_KV_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=([^ ]+)")
_ORDER_SKIP_RE = re.compile(r"\bORDER_SKIP\b.*?\bsymbol=(?P<symbol>[A-Z][A-Z0-9.\-]{0,9})\b.*?\breason=(?P<reason>[^ ]+)")


@dataclass(frozen=True)
class DynamicDispatchExplainabilityPaths:
    json_path: Path
    text_path: Path


def _safe_user(user_id: str) -> str:
    return "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in str(user_id or "default")) or "default"


def _read_text(path: Path) -> str:
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    return path.read_text(encoding="utf-8", errors="replace")


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip().strip("$,")
    if not text or text.lower() in {"n/a", "none", "null"}:
        return None
    try:
        out = float(text)
    except (TypeError, ValueError, OverflowError):
        return None
    return out if math.isfinite(out) else None


def _round(value: Any, ndigits: int = 4) -> float | None:
    number = _safe_float(value)
    return round(number, ndigits) if number is not None else None


def _parse_bool(value: Any) -> bool | None:
    text = str(value or "").strip().lower()
    if text in {"true", "t", "1", "yes", "on"}:
        return True
    if text in {"false", "f", "0", "no", "off"}:
        return False
    return None


def _parse_kv(line: str) -> dict[str, str]:
    return {match.group(1): match.group(2).strip(",;") for match in _KV_RE.finditer(line)}


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


def classify_dispatch_rule(reason: Any) -> str:
    """Classify a dispatcher block as a safety or strategy rule."""

    text = str(reason or "").strip().lower().replace(" ", "_")
    if not text or text == "submitted":
        return "n/a"
    safety_markers = (
        "dynamic_price_below_minimum",
        "spread_cap",
        "spread",
        "bad_quote",
        "unstable_quote",
        "no_quote",
        "quote",
        "relative_volume",
        "vwap",
        "cooldown",
        "exposure",
        "risk",
        "position",
        "pdt",
        "cap",
        "size_below",
        "execution_blocked",
    )
    strategy_markers = (
        "weak_catalyst_dynamic",
        "expectancy_gate",
        "trend_reentry",
        "late_entry",
        "profile_filter",
    )
    if any(marker in text for marker in safety_markers):
        return "safety_rule"
    if any(marker in text for marker in strategy_markers):
        return "strategy_rule"
    return "strategy_rule" if "dynamic" in text else "unknown_rule"


def _blank_row(symbol: str) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "source": None,
        "route": None,
        "action": None,
        "notional": None,
        "scanner_score": None,
        "dynamic_score": None,
        "gain_pct": None,
        "day_gain_pct": None,
        "relative_volume": None,
        "news_score": None,
        "catalyst_score": None,
        "catalyst_fastlane_active": None,
        "weak_catalyst_dynamic": None,
        "entry_eval_final": None,
        "dispatcher_result": None,
        "dispatcher_skip_reason": None,
        "rule_class": None,
        "allocator_action": False,
        "dispatch_started": False,
        "missed_opportunity": {"plus_5m": None, "plus_15m": None, "plus_30m": None, "end_of_day": None},
        "evidence": [],
    }


def _row(rows: dict[str, dict[str, Any]], symbol: str) -> dict[str, Any]:
    sym = str(symbol or "").strip().upper()
    if not sym:
        sym = "?"
    if sym not in rows:
        rows[sym] = _blank_row(sym)
    return rows[sym]


def _note(row: dict[str, Any], path: Path, line_no: int, line: str) -> None:
    if len(row["evidence"]) >= 8:
        return
    row["evidence"].append({"source_file": str(path), "line_number": line_no, "line": line.strip()})


def _coalesce(row: dict[str, Any], key: str, value: Any) -> None:
    if value in (None, "", "n/a"):
        return
    if row.get(key) in (None, "", "n/a"):
        row[key] = value


def _action_value(raw: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = raw.get(key)
        if value is not None and str(value).strip() != "":
            return value
    return None


def _apply_action(row: dict[str, Any], raw: Mapping[str, Any]) -> None:
    row["allocator_action"] = True
    _coalesce(row, "action", _action_value(raw, "action", "side"))
    _coalesce(row, "source", _action_value(raw, "source"))
    _coalesce(row, "route", _action_value(raw, "route", "entry_route"))
    _coalesce(row, "notional", _round(_action_value(raw, "notional", "requested_notional"), 2))
    _coalesce(row, "scanner_score", _round(_action_value(raw, "scanner_score", "score"), 4))
    _coalesce(row, "dynamic_score", _round(_action_value(raw, "dynamic_score", "scanner_score", "score"), 4))
    _coalesce(row, "gain_pct", _round(_action_value(raw, "gain_pct"), 4))
    _coalesce(row, "day_gain_pct", _round(_action_value(raw, "day_gain_pct", "gain_pct"), 4))
    _coalesce(row, "relative_volume", _round(_action_value(raw, "relative_volume", "rel_volume"), 4))
    _coalesce(row, "news_score", _round(_action_value(raw, "news_score"), 4))
    _coalesce(row, "catalyst_score", _round(_action_value(raw, "catalyst_score"), 4))
    _coalesce(row, "catalyst_fastlane_active", _parse_bool(_action_value(raw, "catalyst_fastlane_active", "catalyst_fastlane")))
    _coalesce(row, "weak_catalyst_dynamic", _parse_bool(_action_value(raw, "weak_catalyst_dynamic", "is_weak_catalyst_dynamic")))
    final = _parse_bool(_action_value(raw, "entry_eval_final", "entry_final", "final"))
    _coalesce(row, "entry_eval_final", final)


def _parse_allocator_actions(line: str) -> list[dict[str, Any]]:
    if "ALLOCATOR ACTIONS:" not in line:
        return []
    payload = line.split("ALLOCATOR ACTIONS:", 1)[1].strip()
    try:
        parsed = ast.literal_eval(payload)
    except Exception:
        parsed = None
    if not isinstance(parsed, list):
        return []
    return [dict(item) for item in parsed if isinstance(item, Mapping) and str(item.get("symbol") or "").strip()]


def _parse_action_created(line: str) -> dict[str, Any] | None:
    if "ALLOCATOR_ACTION_CREATED" not in line:
        return None
    kv = _parse_kv(line)
    if not kv.get("symbol"):
        return None
    return {
        "symbol": kv.get("symbol"),
        "action": kv.get("action"),
        "notional": kv.get("notional"),
        "route": kv.get("route"),
        "source": kv.get("source"),
    }


def _apply_dispatch_kv(row: dict[str, Any], kv: Mapping[str, Any], *, result: str | None = None, reason: str | None = None) -> None:
    _coalesce(row, "source", kv.get("source"))
    _coalesce(row, "route", kv.get("route"))
    _coalesce(row, "notional", _round(kv.get("notional"), 2))
    for key in ("scanner_score", "dynamic_score", "gain_pct", "day_gain_pct", "relative_volume", "news_score", "catalyst_score"):
        _coalesce(row, key, _round(kv.get(key), 4))
    for key in ("catalyst_fastlane_active", "weak_catalyst_dynamic", "entry_eval_final"):
        parsed = _parse_bool(kv.get(key))
        _coalesce(row, key, parsed)
    if result:
        row["dispatcher_result"] = result
    if reason:
        row["dispatcher_skip_reason"] = reason
    if row.get("dispatcher_skip_reason"):
        row["rule_class"] = classify_dispatch_rule(row["dispatcher_skip_reason"])


def _discover_log_paths(project_root: Path, *, data_dir: Path, day: str, extra_paths: Sequence[Path | str] | None) -> list[Path]:
    paths: list[Path] = []
    review_dir = data_dir / "review" / day
    if review_dir.exists():
        for pattern in ("*.log", "*.txt", "*.log.gz"):
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


def latest_dynamic_dispatch_explainability_date(
    *,
    project_root: Path | str = ".",
    data_dir: Path | str = "data",
    user_id: str = "live_bot",
) -> str | None:
    del project_root, user_id
    data = Path(data_dir)
    days: set[str] = set()
    for root in (data / "review", data / "logs", data / "debug_logs"):
        if root.exists():
            for path in root.rglob("*"):
                day = _date_from_path(path)
                if day:
                    days.add(day)
    return sorted(days)[-1] if days else None


def _parse_logs(paths: Sequence[Path]) -> tuple[list[dict[str, Any]], list[str]]:
    rows: dict[str, dict[str, Any]] = {}
    used: list[str] = []
    for path in paths:
        try:
            lines = _read_text(path).splitlines()
        except OSError:
            continue
        useful = False
        for line_no, line in enumerate(lines, start=1):
            actions = _parse_allocator_actions(line)
            if actions:
                useful = True
                for raw in actions:
                    symbol = str(raw.get("symbol") or "").strip().upper()
                    candidate = _row(rows, symbol)
                    _apply_action(candidate, raw)
                    _note(candidate, path, line_no, line)
                continue
            action_created = _parse_action_created(line)
            if action_created:
                useful = True
                candidate = _row(rows, str(action_created.get("symbol") or ""))
                _apply_action(candidate, action_created)
                _note(candidate, path, line_no, line)
                continue
            if "ALLOCATOR_DISPATCH_START" in line:
                kv = _parse_kv(line)
                if kv.get("symbol"):
                    useful = True
                    candidate = _row(rows, kv["symbol"])
                    candidate["allocator_action"] = True
                    candidate["dispatch_started"] = True
                    _apply_dispatch_kv(candidate, kv)
                    _note(candidate, path, line_no, line)
                continue
            if "DYNAMIC_DISPATCH_EXPLAINABILITY" in line:
                kv = _parse_kv(line)
                if kv.get("symbol"):
                    useful = True
                    candidate = _row(rows, kv["symbol"])
                    candidate["allocator_action"] = True
                    result = kv.get("dispatcher_result")
                    reason = kv.get("dispatcher_skip_reason")
                    _apply_dispatch_kv(candidate, kv, result=result, reason=reason if reason != "submitted" else None)
                    candidate["rule_class"] = kv.get("rule_class") or candidate.get("rule_class")
                    _note(candidate, path, line_no, line)
                continue
            if "ORDER_SKIP" in line and "source=capital_allocator" in line:
                match = _ORDER_SKIP_RE.search(line)
                kv = _parse_kv(line)
                symbol = (match.group("symbol") if match else kv.get("symbol")) or ""
                reason = (match.group("reason") if match else kv.get("reason")) or "unknown"
                if symbol:
                    useful = True
                    candidate = _row(rows, symbol)
                    candidate["allocator_action"] = True
                    _apply_dispatch_kv(candidate, kv, result="skipped", reason=reason)
                    _note(candidate, path, line_no, line)
                continue
            if "ALLOCATOR_DISPATCH_SKIPPED" in line or "ALLOCATOR_ACTION_BLOCKED" in line:
                kv = _parse_kv(line)
                if kv.get("symbol"):
                    useful = True
                    candidate = _row(rows, kv["symbol"])
                    candidate["allocator_action"] = True
                    _apply_dispatch_kv(candidate, kv, result="skipped", reason=kv.get("reason") or "unknown")
                    _note(candidate, path, line_no, line)
                continue
            if "ALLOCATOR_DISPATCH_DONE" in line or "ALLOCATOR_DISPATCH_END" in line:
                kv = _parse_kv(line)
                if kv.get("symbol"):
                    useful = True
                    candidate = _row(rows, kv["symbol"])
                    candidate["allocator_action"] = True
                    result = kv.get("result") or "unknown"
                    reason = kv.get("reason")
                    _apply_dispatch_kv(candidate, kv, result=result, reason=None if result == "submitted" else reason)
                    _note(candidate, path, line_no, line)
                continue
            if "ORDER_SUBMITTED" in line and "source=capital_allocator" in line:
                kv = _parse_kv(line)
                if kv.get("symbol"):
                    useful = True
                    candidate = _row(rows, kv["symbol"])
                    candidate["allocator_action"] = True
                    _apply_dispatch_kv(candidate, kv, result="submitted")
                    _note(candidate, path, line_no, line)
        if useful:
            used.append(str(path))
    return list(rows.values()), used


def _load_bar_rows(symbol: str, *, day: str, bars_dir: Path | None) -> list[dict[str, Any]]:
    if bars_dir is None or not bars_dir.exists():
        return []
    candidates = [
        bars_dir / f"{symbol}.csv",
        bars_dir / f"{day}_{symbol}.csv",
        bars_dir / day / f"{symbol}.csv",
        bars_dir / day / f"{symbol}.json",
    ]
    path = next((item for item in candidates if item.exists() and item.is_file()), None)
    if path is None:
        return []
    try:
        if path.suffix == ".json":
            parsed = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(parsed, list):
                return [dict(item) for item in parsed if isinstance(item, Mapping)]
            return []
        with path.open("r", encoding="utf-8", errors="replace", newline="") as fh:
            return [dict(row) for row in csv.DictReader(fh)]
    except (OSError, json.JSONDecodeError, csv.Error):
        return []


def _parse_bar_time(row: Mapping[str, Any]) -> datetime | None:
    raw = row.get("timestamp") or row.get("time") or row.get("ts") or row.get("datetime")
    if raw in (None, ""):
        return None
    text = str(raw).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _estimate_forward_returns(row: Mapping[str, Any], *, day: str, bars_dir: Path | None) -> dict[str, float | None]:
    empty = {"plus_5m": None, "plus_15m": None, "plus_30m": None, "end_of_day": None}
    if row.get("dispatcher_result") != "skipped":
        return empty
    symbol = str(row.get("symbol") or "").upper()
    bars = _load_bar_rows(symbol, day=day, bars_dir=bars_dir)
    if not bars:
        return empty
    points: list[tuple[datetime, float]] = []
    for raw in bars:
        ts = _parse_bar_time(raw)
        close = _safe_float(raw.get("close") or raw.get("c"))
        if ts is not None and close is not None and close > 0.0:
            points.append((ts, close))
    if len(points) < 2:
        return empty
    points.sort(key=lambda item: item[0])
    entry = _safe_float(row.get("observed_price")) or _safe_float(row.get("price")) or points[0][1]
    if entry <= 0.0:
        return empty
    start = points[0][0]

    def at_minutes(minutes: int) -> float | None:
        target = start.timestamp() + minutes * 60
        future = next((close for ts, close in points if ts.timestamp() >= target), None)
        return round(((future / entry) - 1.0) * 100.0, 4) if future else None

    eod = points[-1][1]
    return {
        "plus_5m": at_minutes(5),
        "plus_15m": at_minutes(15),
        "plus_30m": at_minutes(30),
        "end_of_day": round(((eod / entry) - 1.0) * 100.0, 4),
    }


def _is_high_score(row: Mapping[str, Any]) -> bool:
    score = _safe_float(row.get("dynamic_score")) or _safe_float(row.get("scanner_score")) or 0.0
    return score >= 0.8 or score >= 80.0


def _finalize_rows(rows: Sequence[dict[str, Any]], *, day: str, bars_dir: Path | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for raw in rows:
        row = dict(raw)
        if row.get("dispatcher_result") is None and row.get("dispatcher_skip_reason"):
            row["dispatcher_result"] = "skipped"
        if row.get("dispatcher_result") is None and row.get("dispatch_started"):
            row["dispatcher_result"] = "unknown"
        if row.get("dispatcher_skip_reason") and row.get("rule_class") in (None, "n/a"):
            row["rule_class"] = classify_dispatch_rule(row["dispatcher_skip_reason"])
        row["missed_opportunity"] = _estimate_forward_returns(row, day=day, bars_dir=bars_dir)
        out.append(row)
    out.sort(key=lambda item: str(item.get("symbol") or ""))
    return out


def build_dynamic_dispatch_explainability_report(
    *,
    project_root: Path | str = ".",
    data_dir: Path | str = "data",
    day: str,
    user_id: str = "live_bot",
    log_paths: Sequence[Path | str] | None = None,
    bars_dir: Path | str | None = None,
) -> dict[str, Any]:
    root = Path(project_root)
    data = Path(data_dir)
    if day == "latest":
        latest = latest_dynamic_dispatch_explainability_date(project_root=root, data_dir=data, user_id=user_id)
        day = latest or day
    discovered = _discover_log_paths(root, data_dir=data, day=day, extra_paths=log_paths)
    parsed_rows, used = _parse_logs(discovered)
    bars = Path(bars_dir) if bars_dir is not None else None
    rows = _finalize_rows(parsed_rows, day=day, bars_dir=bars)
    skipped = [row for row in rows if row.get("dispatcher_result") in {"skipped", "blocked", "error"}]
    submitted = [row for row in rows if row.get("dispatcher_result") == "submitted"]
    skip_reasons = Counter(str(row.get("dispatcher_skip_reason") or "unknown") for row in skipped)
    symbols_blocked = Counter(str(row.get("symbol") or "?") for row in skipped)
    high_score_blocks = [row for row in skipped if _is_high_score(row)]
    fastlane_blocks = [row for row in skipped if row.get("catalyst_fastlane_active") is True]
    weak_blocks = [row for row in skipped if row.get("weak_catalyst_dynamic") is True]
    return {
        "report": "dynamic_dispatch_explainability",
        "research_only": True,
        "date": day,
        "user": user_id,
        "source_files": used,
        "summary": {
            "total_allocator_actions": sum(1 for row in rows if row.get("allocator_action")),
            "submitted_orders": len(submitted),
            "skipped_or_blocked_orders": len(skipped),
            "top_dispatcher_skip_reasons": dict(skip_reasons.most_common()),
            "symbols_most_frequently_blocked_after_allocation": dict(symbols_blocked.most_common()),
            "high_score_dynamic_blocks": len(high_score_blocks),
            "catalyst_fastlane_blocks": len(fastlane_blocks),
            "weak_catalyst_dynamic_blocks": len(weak_blocks),
        },
        "rules_blocking_high_score_dynamic_candidates": [
            {"symbol": row["symbol"], "reason": row.get("dispatcher_skip_reason"), "rule_class": row.get("rule_class")}
            for row in high_score_blocks
        ],
        "rules_blocking_catalyst_fastlane_candidates": [
            {"symbol": row["symbol"], "reason": row.get("dispatcher_skip_reason"), "rule_class": row.get("rule_class")}
            for row in fastlane_blocks
        ],
        "rules_blocking_weak_catalyst_dynamic_candidates": [
            {"symbol": row["symbol"], "reason": row.get("dispatcher_skip_reason"), "rule_class": row.get("rule_class")}
            for row in weak_blocks
        ],
        "sample_rows": rows[:25],
        "rows": rows,
    }


def render_dynamic_dispatch_explainability_report(report: Mapping[str, Any]) -> str:
    summary = report.get("summary") if isinstance(report.get("summary"), Mapping) else {}
    lines = [
        "DYNAMIC DISPATCH EXPLAINABILITY REPORT",
        f"date: {report.get('date')}",
        f"user: {report.get('user')}",
        f"source_files: {len(report.get('source_files') or [])}",
        "",
        "SUMMARY",
        f"total_allocator_actions: {summary.get('total_allocator_actions', 0)}",
        f"submitted_orders: {summary.get('submitted_orders', 0)}",
        f"skipped_or_blocked_orders: {summary.get('skipped_or_blocked_orders', 0)}",
        f"top_dispatcher_skip_reasons: {summary.get('top_dispatcher_skip_reasons', {})}",
        f"symbols_most_frequently_blocked_after_allocation: {summary.get('symbols_most_frequently_blocked_after_allocation', {})}",
        f"high_score_dynamic_blocks: {summary.get('high_score_dynamic_blocks', 0)}",
        f"catalyst_fastlane_blocks: {summary.get('catalyst_fastlane_blocks', 0)}",
        f"weak_catalyst_dynamic_blocks: {summary.get('weak_catalyst_dynamic_blocks', 0)}",
        "",
        "SAMPLE ROWS",
    ]
    for row in report.get("sample_rows") or []:
        if not isinstance(row, Mapping):
            continue
        lines.append(
            "symbol={symbol} route={route} source={source} notional={notional} "
            "scanner_score={scanner_score} dynamic_score={dynamic_score} gain_pct={gain_pct} "
            "relative_volume={relative_volume} news_score={news_score} catalyst_score={catalyst_score} "
            "fastlane={catalyst_fastlane_active} weak_catalyst_dynamic={weak_catalyst_dynamic} "
            "entry_eval_final={entry_eval_final} dispatcher_result={dispatcher_result} "
            "dispatcher_skip_reason={dispatcher_skip_reason} rule_class={rule_class} "
            "missed_opportunity={missed_opportunity}".format(**row)
        )
    return "\n".join(lines)


def write_dynamic_dispatch_explainability_report(
    *,
    project_root: Path | str = ".",
    data_dir: Path | str = "data",
    day: str,
    user_id: str = "live_bot",
    log_paths: Sequence[Path | str] | None = None,
    bars_dir: Path | str | None = None,
) -> tuple[Path, Path, dict[str, Any]]:
    report = build_dynamic_dispatch_explainability_report(
        project_root=project_root,
        data_dir=data_dir,
        day=day,
        user_id=user_id,
        log_paths=log_paths,
        bars_dir=bars_dir,
    )
    data = Path(data_dir)
    out_dir = data / "review" / str(report["date"])
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_user = _safe_user(user_id)
    json_path = out_dir / f"{safe_user}_dynamic_dispatch_explainability_report.json"
    text_path = out_dir / f"{safe_user}_dynamic_dispatch_explainability_report.txt"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    text_path.write_text(render_dynamic_dispatch_explainability_report(report) + "\n", encoding="utf-8")
    return json_path, text_path, report
