"""Research-only analysis for dynamic candidates blocked by history bars."""

from __future__ import annotations

import json
import math
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import timezone
from pathlib import Path
from typing import Any

import pandas as pd

from src.dynamic_candidate_blockers import (
    _discover_log_paths,
    _line_timestamp,
    _load_local_bars_with_diagnostics,
    _parse_kv,
    _parse_timestamp,
    _read_text,
    _safe_float,
    _safe_user,
)

_THRESHOLDS = (200, 180, 160, 150, 120)
_ACCEPT_RE = re.compile(r"\bDYNAMIC_SCAN\s+accept\s+(?P<symbol>[A-Z][A-Z0-9.\-]{0,9})\b")
_SELECTED_RE = re.compile(r"\bDYNAMIC_SELECTED\s+symbol=(?P<symbol>[A-Z][A-Z0-9.\-]{0,9})\b")
_HISTORY_SKIP_RE = re.compile(
    r"\b(?:SKIP|DYNAMIC_NOT_TRADABLE)\s+(?P<symbol>[A-Z][A-Z0-9.\-]{0,9}):?\s+"
    r"(?:reason=)?(?P<reason>[^\n]*?not enough bars\s+\(got\s+(?P<got>\d+),\s+need\s+(?P<need>\d+)\)[^\n]*)",
    re.IGNORECASE,
)
_ENTRY_EVAL_RE = re.compile(r"\b(?P<symbol>[A-Z][A-Z0-9.\-]{0,9})\s+ENTRY_EVAL\b(?P<body>.*)$")


def _round(value: Any, ndigits: int = 4) -> float | None:
    number = _safe_float(value)
    return round(number, ndigits) if number is not None else None


def _history_paths(data_dir: Path, *, day: str, user_id: str) -> list[Path]:
    root = data_dir / "dynamic_scan_history"
    if not root.exists():
        return []
    safe = _safe_user(user_id)
    exact: list[Path] = []
    fallback: list[Path] = []
    compact = day.replace("-", "")
    for path in sorted(root.glob("*.json")):
        if day not in path.name and compact not in path.name:
            continue
        if path.name.endswith(f"_{safe}.json"):
            exact.append(path)
        elif path.name.endswith("_default.json"):
            fallback.append(path)
    return exact or fallback


def _scan_history_state(
    *,
    data_dir: Path,
    day: str,
    user_id: str,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]], dict[str, dict[str, Any]], list[str]]:
    accepted: dict[str, list[dict[str, Any]]] = defaultdict(list)
    selected: dict[str, list[dict[str, Any]]] = defaultdict(list)
    snapshots: dict[str, dict[str, Any]] = {}
    used: list[str] = []
    for path in _history_paths(data_dir, day=day, user_id=user_id):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(payload, Mapping):
            continue
        used.append(str(path))
        generated_at = _parse_timestamp(payload.get("generated_at"))
        generated_iso = generated_at.isoformat() if generated_at is not None else None
        selected_symbols = {str(sym).strip().upper() for sym in payload.get("selected") or [] if str(sym).strip()}
        candidates = payload.get("candidates")
        if not isinstance(candidates, list):
            candidates = (payload.get("accepted") or []) + (payload.get("rejected") or [])
        for raw in candidates or []:
            if not isinstance(raw, Mapping):
                continue
            symbol = str(raw.get("symbol") or "").strip().upper()
            if not symbol:
                continue
            row_ts = _parse_timestamp(raw.get("timestamp") or raw.get("scan_timestamp")) or generated_at
            row = {
                "symbol": symbol,
                "timestamp": row_ts.isoformat() if row_ts is not None else generated_iso,
                "price": _round(raw.get("price")),
                "score": _round(raw.get("score")),
                "source_file": str(path),
            }
            snapshots[symbol] = {**snapshots.get(symbol, {}), **row}
            if bool(raw.get("accepted")):
                accepted[symbol].append(row)
            if symbol in selected_symbols:
                selected[symbol].append(row)
    return accepted, selected, snapshots, used


def _append_event(target: dict[str, list[dict[str, Any]]], symbol: str, row: Mapping[str, Any]) -> None:
    target[str(symbol or "").strip().upper()].append(dict(row))


def _ingest_logs(
    *,
    paths: Sequence[Path],
    day: str,
    accepted: dict[str, list[dict[str, Any]]],
    selected: dict[str, list[dict[str, Any]]],
    snapshots: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]], list[str]]:
    history_blocks: list[dict[str, Any]] = []
    downstream: dict[str, list[dict[str, Any]]] = defaultdict(list)
    used: list[str] = []
    for path in paths:
        try:
            lines = _read_text(path).splitlines()
        except Exception:
            continue
        used.append(str(path))
        for line_no, line in enumerate(lines, start=1):
            ts = _line_timestamp(line, day=day)
            ts_iso = ts.isoformat() if ts is not None else None
            accept = _ACCEPT_RE.search(line)
            if accept is not None:
                symbol = accept.group("symbol").upper()
                row = {"symbol": symbol, "timestamp": ts_iso, "source_file": str(path), "line_number": line_no}
                _append_event(accepted, symbol, row)
                snapshots[symbol] = {**snapshots.get(symbol, {}), **row}
                continue
            selected_match = _SELECTED_RE.search(line)
            if selected_match is not None:
                symbol = selected_match.group("symbol").upper()
                kv = _parse_kv(line)
                row = {
                    "symbol": symbol,
                    "timestamp": ts_iso,
                    "score": _round(kv.get("score")),
                    "source_file": str(path),
                    "line_number": line_no,
                }
                _append_event(selected, symbol, row)
                snapshots[symbol] = {**snapshots.get(symbol, {}), **row}
                continue
            skip = _HISTORY_SKIP_RE.search(line)
            if skip is not None:
                symbol = skip.group("symbol").upper()
                history_blocks.append(
                    {
                        "symbol": symbol,
                        "timestamp": ts_iso,
                        "got_bars": int(skip.group("got")),
                        "required_bars": int(skip.group("need")),
                        "reason": skip.group("reason").strip(),
                        "source_file": str(path),
                        "line_number": line_no,
                        "line": line.strip(),
                    }
                )
                continue
            entry = _ENTRY_EVAL_RE.search(line)
            if entry is not None:
                symbol = entry.group("symbol").upper()
                kv = _parse_kv(entry.group("body"))
                downstream[symbol].append(
                    {
                        "stage": "ENTRY_EVAL",
                        "timestamp": ts_iso,
                        "route": kv.get("route"),
                        "final": kv.get("final"),
                        "reason": kv.get("reason"),
                        "source_file": str(path),
                        "line_number": line_no,
                        "line": line.strip(),
                    }
                )
            elif any(token in line for token in ("ORDER", "ALLOCATOR", "BUY", "SELL")):
                for symbol in set(accepted) | set(selected):
                    if re.search(rf"\b{re.escape(symbol)}\b", line):
                        downstream[symbol].append(
                            {
                                "stage": "downstream_log",
                                "timestamp": ts_iso,
                                "source_file": str(path),
                                "line_number": line_no,
                                "line": line.strip(),
                            }
                        )
    return history_blocks, downstream, used


def _event_before(events: Sequence[Mapping[str, Any]], timestamp: Any) -> Mapping[str, Any] | None:
    if not events:
        return None
    block_ts = _parse_timestamp(timestamp)
    if block_ts is None:
        return events[-1]
    prior: list[Mapping[str, Any]] = []
    for event in events:
        event_ts = _parse_timestamp(event.get("timestamp"))
        if event_ts is None or event_ts <= block_ts:
            prior.append(event)
    return prior[-1] if prior else None


def _close_col(bars: pd.DataFrame) -> str | None:
    return next((col for col in ("close", "Close", "c") if col in bars.columns), None)


def _timestamps_utc(bars: pd.DataFrame) -> pd.Series | None:
    if bars.empty:
        return None
    if isinstance(bars.index, pd.DatetimeIndex):
        parsed = pd.Series(bars.index, index=bars.index)
    else:
        parsed = None
        for col in ("timestamp", "datetime", "time", "ts", "t", "date"):
            if col in bars.columns:
                parsed = pd.to_datetime(bars[col], utc=True, errors="coerce")
                break
        if parsed is None:
            return None
    parsed = pd.to_datetime(parsed, utc=True, errors="coerce")
    return parsed if parsed.notna().any() else None


def _forward_returns(
    bars: pd.DataFrame | None,
    *,
    selected_at: Any,
    base_price: Any,
    day: str,
) -> dict[str, Any]:
    empty = {
        "forward_returns_available": False,
        "missing_forward_bar_reason": None,
        "return_5m_pct": None,
        "return_10m_pct": None,
        "return_20m_pct": None,
        "return_30m_pct": None,
    }
    selected_ts = _parse_timestamp(selected_at)
    if selected_ts is None:
        return {**empty, "missing_forward_bar_reason": "missing_selection_timestamp"}
    if bars is None or bars.empty:
        return {**empty, "missing_forward_bar_reason": "missing_local_bars"}
    close_col = _close_col(bars)
    timestamps = _timestamps_utc(bars)
    if close_col is None:
        return {**empty, "missing_forward_bar_reason": "missing_close_column"}
    if timestamps is None:
        return {**empty, "missing_forward_bar_reason": "missing_bar_timestamps"}
    work = bars.copy()
    work["_ts_utc"] = timestamps
    selected_utc = selected_ts.astimezone(timezone.utc)
    same_day = work["_ts_utc"].dt.tz_convert("America/New_York").dt.date.astype(str) == day
    later = work.loc[same_day & (work["_ts_utc"] >= selected_utc)].copy()
    if later.empty:
        return {**empty, "missing_forward_bar_reason": "no_bars_after_selection"}
    closes = pd.to_numeric(later[close_col], errors="coerce").dropna()
    if closes.empty:
        return {**empty, "missing_forward_bar_reason": "missing_close_values"}
    base = _safe_float(base_price)
    if base is None or base <= 0:
        base = float(closes.iloc[0])
    out = dict(empty)
    available = 0
    for minutes in (5, 10, 20, 30):
        horizon = later.loc[later["_ts_utc"] >= selected_utc + pd.Timedelta(minutes=minutes)]
        value = None
        if not horizon.empty:
            close = _safe_float(horizon.iloc[0].get(close_col))
            if close is not None and base > 0:
                value = round(((float(close) / float(base)) - 1.0) * 100.0, 4)
                available += 1
        out[f"return_{minutes}m_pct"] = value
    out["forward_returns_available"] = available > 0
    out["missing_forward_bar_reason"] = None if available > 0 else "no_horizon_bars_after_selection"
    return out


def _threshold_comparison(rows: Sequence[Mapping[str, Any]], thresholds: Sequence[int] = _THRESHOLDS) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for threshold in thresholds:
        eligible = sorted(
            {
                str(row.get("symbol") or "")
                for row in rows
                if _safe_float(row.get("got_bars")) is not None and float(row.get("got_bars")) >= float(threshold)
            }
        )
        out[str(threshold)] = {"threshold": threshold, "eligible_count": len(eligible), "symbols": eligible}
    return out


def _build_rows(
    *,
    data_dir: Path,
    day: str,
    accepted: Mapping[str, Sequence[Mapping[str, Any]]],
    selected: Mapping[str, Sequence[Mapping[str, Any]]],
    snapshots: Mapping[str, Mapping[str, Any]],
    history_blocks: Sequence[Mapping[str, Any]],
    downstream: Mapping[str, Sequence[Mapping[str, Any]]],
    bars_dir: Path | str | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    bars_cache: dict[str, tuple[pd.DataFrame | None, dict[str, Any]]] = {}
    selected_or_accepted = set(accepted) | set(selected)
    for block in history_blocks:
        symbol = str(block.get("symbol") or "").upper()
        if symbol not in selected_or_accepted:
            continue
        selected_event = _event_before(selected.get(symbol, []), block.get("timestamp"))
        accepted_event = _event_before(accepted.get(symbol, []), block.get("timestamp"))
        if selected_event is None and accepted_event is None:
            continue
        basis = selected_event or accepted_event or snapshots.get(symbol, {})
        if symbol not in bars_cache:
            bars_cache[symbol] = _load_local_bars_with_diagnostics(
                data_dir=data_dir,
                bars_dir=bars_dir,
                symbol=symbol,
                day=day,
            )
        bars, bar_diag = bars_cache[symbol]
        price = _safe_float(snapshots.get(symbol, {}).get("price")) or _safe_float(basis.get("price"))
        got = int(block.get("got_bars") or 0)
        required = int(block.get("required_bars") or 0)
        forward = _forward_returns(
            bars,
            selected_at=basis.get("timestamp") or block.get("timestamp"),
            base_price=price,
            day=day,
        )
        if forward.get("missing_forward_bar_reason") == "missing_local_bars" and bar_diag.get("missing_bar_reason"):
            forward["missing_forward_bar_reason"] = bar_diag.get("missing_bar_reason")
        rows.append(
            {
                "symbol": symbol,
                "selected": selected_event is not None,
                "accepted": accepted_event is not None,
                "all_other_dynamic_gates_passed": selected_event is not None or accepted_event is not None,
                "selected_time": None if selected_event is None else selected_event.get("timestamp"),
                "accepted_time": None if accepted_event is None else accepted_event.get("timestamp"),
                "blocked_time": block.get("timestamp"),
                "got_bars": got,
                "required_bars": required,
                "got_required_ratio": round(got / required, 4) if required > 0 else None,
                "would_pass_current_200": got >= 200,
                "rejection_reason": block.get("reason"),
                "source_file": block.get("source_file"),
                "line_number": block.get("line_number"),
                "price": _round(price),
                "bar_diagnostics": bar_diag,
                "forward_returns": forward,
                "downstream_events": list(downstream.get(symbol, [])),
            }
        )
    rows.sort(key=lambda row: (row.get("blocked_time") or "", row.get("symbol") or ""))
    diagnostics = {
        symbol: diag
        for symbol, (_bars, diag) in sorted(bars_cache.items())
    }
    return rows, diagnostics


def latest_dynamic_history_gate_date(
    *,
    data_dir: Path | str = "data",
    user_id: str = "live_bot",
) -> str | None:
    root = Path(data_dir) / "dynamic_scan_history"
    if not root.exists():
        return None
    safe = _safe_user(user_id)
    dates: set[str] = set()
    for path in root.glob("*.json"):
        if not (path.name.endswith(f"_{safe}.json") or path.name.endswith("_default.json")):
            continue
        match = re.search(r"(\d{8})", path.name)
        if match:
            raw = match.group(1)
            dates.add(f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}")
    return sorted(dates)[-1] if dates else None


def build_dynamic_history_gate_research_report(
    *,
    project_root: Path | str = ".",
    data_dir: Path | str = "data",
    day: str,
    user_id: str = "live_bot",
    log_paths: Sequence[Path | str] | None = None,
    bars_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Build a read-only report for selected/accepted dynamic candidates blocked by 200-bar history."""
    data = Path(data_dir)
    if day == "latest":
        latest = latest_dynamic_history_gate_date(data_dir=data, user_id=user_id)
        if latest is None:
            raise FileNotFoundError("No dynamic scan-history date found")
        day = latest
    accepted, selected, snapshots, history_used = _scan_history_state(data_dir=data, day=day, user_id=user_id)
    logs = _discover_log_paths(Path(project_root), day=day, extra_paths=log_paths)
    history_blocks, downstream, logs_used = _ingest_logs(
        paths=logs,
        day=day,
        accepted=accepted,
        selected=selected,
        snapshots=snapshots,
    )
    rows, bar_diagnostics = _build_rows(
        data_dir=data,
        day=day,
        accepted=accepted,
        selected=selected,
        snapshots=snapshots,
        history_blocks=history_blocks,
        downstream=downstream,
        bars_dir=bars_dir,
    )
    unique_symbols = sorted({str(row.get("symbol") or "") for row in rows if row.get("symbol")})
    selected_blocked = sorted({str(row.get("symbol") or "") for row in rows if row.get("selected")})
    return {
        "report": "dynamic_history_gate_research",
        "research_only": True,
        "date": day,
        "user": user_id,
        "source_files": sorted(set(history_used + logs_used)),
        "accepted_symbols": sorted(accepted),
        "selected_symbols": sorted(selected),
        "history_blocked_candidates": rows,
        "threshold_comparison": _threshold_comparison(rows),
        "bar_diagnostics": bar_diagnostics,
        "summary": {
            "accepted_symbols": len(accepted),
            "selected_symbols": len(selected),
            "history_blocked_events": len(rows),
            "history_blocked_symbols": unique_symbols,
            "selected_history_blocked_symbols": selected_blocked,
            "selected_history_blocked_count": len(selected_blocked),
            "outcomes_available": sum(1 for row in rows if row.get("forward_returns", {}).get("forward_returns_available")),
            "missing_forward_bars": sum(1 for row in rows if not row.get("forward_returns", {}).get("forward_returns_available")),
        },
    }


def render_dynamic_history_gate_research_report(report: Mapping[str, Any]) -> str:
    summary = report.get("summary") if isinstance(report.get("summary"), Mapping) else {}
    rows = report.get("history_blocked_candidates") if isinstance(report.get("history_blocked_candidates"), list) else []
    lines = [
        f"Dynamic History Gate Research - {report.get('date')} user={report.get('user')}",
        "Research-only: No trading behavior, thresholds, sizing, risk, allocator, or order logic changed.",
        "",
        "Summary",
        f"- accepted symbols: {summary.get('accepted_symbols', 0)}",
        f"- selected symbols: {summary.get('selected_symbols', 0)}",
        f"- history-blocked events: {summary.get('history_blocked_events', 0)}",
        f"- selected history-blocked symbols: {summary.get('selected_history_blocked_count', 0)} ({', '.join(summary.get('selected_history_blocked_symbols') or []) or 'none'})",
        f"- outcomes available: {summary.get('outcomes_available', 0)}",
        f"- missing/inconclusive forward bars: {summary.get('missing_forward_bars', 0)}",
        "",
        "Blocked Candidates",
    ]
    if not rows:
        lines.append("- none")
    for row in rows:
        fwd = row.get("forward_returns") if isinstance(row.get("forward_returns"), Mapping) else {}
        lines.append(
            "- {symbol} got={got_bars} need={required_bars} ratio={got_required_ratio} "
            "selected={selected} accepted={accepted} all_other_gates_passed={all_other_dynamic_gates_passed} "
            "ret_5/10/20/30={r5}/{r10}/{r20}/{r30} missing={missing}".format(
                symbol=row.get("symbol"),
                got_bars=row.get("got_bars"),
                required_bars=row.get("required_bars"),
                got_required_ratio=row.get("got_required_ratio"),
                selected=row.get("selected"),
                accepted=row.get("accepted"),
                all_other_dynamic_gates_passed=row.get("all_other_dynamic_gates_passed"),
                r5=fwd.get("return_5m_pct"),
                r10=fwd.get("return_10m_pct"),
                r20=fwd.get("return_20m_pct"),
                r30=fwd.get("return_30m_pct"),
                missing=fwd.get("missing_forward_bar_reason"),
            )
        )
    lines.append("")
    lines.append("Hypothetical History Thresholds")
    comparison = report.get("threshold_comparison") if isinstance(report.get("threshold_comparison"), Mapping) else {}
    for threshold in _THRESHOLDS:
        item = comparison.get(str(threshold)) if isinstance(comparison.get(str(threshold)), Mapping) else {}
        lines.append(
            f"- {threshold}: eligible={item.get('eligible_count', 0)} symbols={', '.join(item.get('symbols') or []) or 'none'}"
        )
    lines.append("")
    lines.append("Forward returns use local bar files when available; missing bar data is inconclusive.")
    return "\n".join(lines) + "\n"


def write_dynamic_history_gate_research_report(
    *,
    project_root: Path | str = ".",
    data_dir: Path | str = "data",
    day: str,
    user_id: str = "live_bot",
    log_paths: Sequence[Path | str] | None = None,
    bars_dir: Path | str | None = None,
) -> tuple[Path, Path, dict[str, Any]]:
    """Write JSON and Markdown dynamic history-gate research artifacts."""
    report = build_dynamic_history_gate_research_report(
        project_root=project_root,
        data_dir=data_dir,
        day=day,
        user_id=user_id,
        log_paths=log_paths,
        bars_dir=bars_dir,
    )
    data = Path(data_dir)
    out_dir = data / "research" / "dynamic_history_gate"
    out_dir.mkdir(parents=True, exist_ok=True)
    safe = _safe_user(user_id)
    day_out = str(report.get("date") or day)
    json_path = out_dir / f"{day_out}_{safe}.json"
    md_path = out_dir / f"{day_out}_{safe}.md"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    md_path.write_text(render_dynamic_history_gate_research_report(report), encoding="utf-8")
    return json_path, md_path, report
