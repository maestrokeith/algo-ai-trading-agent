"""Research-only diagnostics for dynamic universe to entry-eval gaps."""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from src.dynamic_conversion_report import (
    _date_from_path,
    _discover_log_paths,
    _parse_kv,
    _read_text,
    _safe_float,
    build_dynamic_conversion_report,
    latest_dynamic_conversion_date,
)

_FOCUS_SYMBOLS = ("RKLZ", "DSY", "INTC", "NOK", "RZLV", "ASTN", "CIIT")
_SYMBOL_RE = re.compile(r"\bsymbol=(?P<symbol>[A-Z][A-Z0-9.\-]{0,9})\b")


@dataclass(frozen=True)
class DynamicEntryGapPaths:
    json_path: Path
    text_path: Path


def _safe_user(user_id: str) -> str:
    return "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in str(user_id or "default")) or "default"


def _symbol_from_line(line: str, kv: Mapping[str, str]) -> str | None:
    sym = str(kv.get("symbol") or "").strip().upper()
    if sym:
        return sym
    match = _SYMBOL_RE.search(line)
    return match.group("symbol").upper() if match else None


def _reason_from_line(line: str, kv: Mapping[str, str]) -> str | None:
    if kv.get("reason"):
        return kv.get("reason")
    match = re.search(r"reason=(?P<reason>.+?)(?=\s+[A-Za-z_][A-Za-z0-9_]*=|$)", line)
    if match:
        return match.group("reason").strip()
    return None


def _selected_times(row: Mapping[str, Any]) -> tuple[str | None, str | None]:
    times = [
        str(event.get("timestamp"))
        for event in row.get("events") or []
        if isinstance(event, Mapping)
        and event.get("stage") == "DYNAMIC_SELECTED"
        and event.get("timestamp")
    ]
    return (min(times), max(times)) if times else (None, None)


def _load_existing_conversion_artifact(data_dir: Path, *, day: str, user_id: str) -> dict[str, Any] | None:
    path = data_dir / "research" / "dynamic_conversion" / f"{day}_{_safe_user(user_id)}.json"
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _log_evidence(paths: Sequence[Path]) -> dict[str, dict[str, Any]]:
    evidence: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "removed_by_top_n_scoring": False,
            "skipped_by_trend_prefilter": False,
            "skipped_by_history_bars": False,
            "skipped_by_cooldown_or_position": False,
            "missing_from_scoring_candidate_set": False,
            "dynamic_symbols_not_in_scoring_candidate_set": False,
            "selected_entry_trace_seen": False,
            "selected_entry_skip_reason": None,
            "history_detail": None,
            "trend_prefilter_detail": None,
            "cooldown_position_detail": None,
            "top_n_detail": None,
            "evidence_lines": [],
        }
    )
    for path in paths:
        try:
            lines = _read_text(path).splitlines()
        except OSError:
            continue
        for line_no, line in enumerate(lines, start=1):
            markers = (
                "DYNAMIC_SELECTED_ENTRY_TRACE",
                "DYNAMIC_SELECTED_ENTRY_DROP",
                "DYNAMIC_SELECTED_ENTRY_SKIPPED",
                "DYNAMIC_SELECTED_DROPPED",
                "DYNAMIC_NOT_TRADABLE",
                "DYNAMIC_MOMENTUM_RANK",
                "DYNAMIC_HIGH_CONVICTION_TREND_PREFILTER_BLOCKED",
                "not in scoring top_n_candidates set",
                "not_in_scoring_top_n_candidates",
                "not enough bars",
                "cooldown",
                "already in positions",
                "open order pending",
                "tracked_state",
                "position_size_above_max",
                "symbol_allocation_cap",
            )
            if not any(marker in line for marker in markers):
                continue
            kv = _parse_kv(line)
            sym = _symbol_from_line(line, kv)
            if not sym:
                continue
            ev = evidence[sym]
            compact = {"source": str(path), "line_number": line_no, "line": line.strip()}
            reason = _reason_from_line(line, kv) or ""
            if "DYNAMIC_SELECTED_ENTRY_TRACE" in line:
                ev["selected_entry_trace_seen"] = True
            if "DYNAMIC_SELECTED_ENTRY_SKIPPED" in line or "DYNAMIC_SELECTED_DROPPED" in line:
                ev["selected_entry_skip_reason"] = reason or ev["selected_entry_skip_reason"]
            reason_l = f"{reason} {line}".lower()
            if "not_in_scoring_top_n_candidates" in reason_l or "not in scoring top_n_candidates set" in reason_l:
                ev["removed_by_top_n_scoring"] = True
                ev["missing_from_scoring_candidate_set"] = True
                ev["dynamic_symbols_not_in_scoring_candidate_set"] = True
                ev["top_n_detail"] = reason or "not_in_scoring_top_n_candidates"
            if "dynamic_momentum_rank_block" in reason_l or "momentum_rank" in reason_l and "blocked" in reason_l:
                ev["removed_by_top_n_scoring"] = True
                ev["top_n_detail"] = reason or "dynamic_momentum_rank_block"
            if "trend_prefilter" in reason_l or "below mas" in reason_l:
                ev["skipped_by_trend_prefilter"] = True
                ev["trend_prefilter_detail"] = reason or "trend_prefilter"
            if "short_history" in reason_l or "not enough bars" in reason_l:
                ev["skipped_by_history_bars"] = True
                ev["history_detail"] = reason or "short_history"
            if any(
                token in reason_l
                for token in (
                    "cooldown",
                    "already_in_positions",
                    "already in positions",
                    "open_order_pending",
                    "open order pending",
                    "tracked_state",
                    "position_size_above_max",
                    "symbol_allocation_cap",
                )
            ):
                ev["skipped_by_cooldown_or_position"] = True
                ev["cooldown_position_detail"] = reason or "cooldown_or_position_constraint"
            ev["evidence_lines"].append(compact)
    return dict(evidence)


def _infer_reason(row: Mapping[str, Any], ev: Mapping[str, Any]) -> str:
    if ev.get("removed_by_top_n_scoring"):
        return str(ev.get("top_n_detail") or "not_in_scoring_top_n_candidates")
    if ev.get("skipped_by_trend_prefilter"):
        return str(ev.get("trend_prefilter_detail") or "trend_prefilter")
    if ev.get("skipped_by_history_bars"):
        return str(ev.get("history_detail") or "short_history")
    if ev.get("skipped_by_cooldown_or_position"):
        return str(ev.get("cooldown_position_detail") or "cooldown_or_position_constraint")
    skip_reason = ev.get("selected_entry_skip_reason") or row.get("inferred_drop_reason")
    if skip_reason and str(skip_reason) != "dynamic_universe_added_but_no_entry_eval_observed":
        return str(skip_reason)
    if row.get("appeared_in_dynamic_universe") and not row.get("reached_entry_eval"):
        return "missing_logging_after_dynamic_universe_added"
    if int(row.get("selection_count") or 0) > 0 and not row.get("appeared_in_dynamic_universe"):
        return "selected_but_not_in_effective_universe"
    return "not_applicable_or_reached_entry_eval"


def _gap_row(row: Mapping[str, Any], evidence: Mapping[str, Any]) -> dict[str, Any]:
    first_selected, last_selected = _selected_times(row)
    ev = evidence.get(str(row.get("symbol") or "").upper(), {})
    return {
        "symbol": row.get("symbol"),
        "selected_count": row.get("selection_count", 0),
        "first_selected_time": first_selected,
        "last_selected_time": last_selected,
        "dynamic_score": row.get("dynamic_score"),
        "entered_effective_universe": bool(row.get("appeared_in_dynamic_universe")),
        "removed_by_top_n_scoring": bool(ev.get("removed_by_top_n_scoring")),
        "skipped_by_trend_prefilter": bool(ev.get("skipped_by_trend_prefilter")),
        "skipped_by_history_bars": bool(ev.get("skipped_by_history_bars")),
        "skipped_by_cooldown_or_position": bool(ev.get("skipped_by_cooldown_or_position")),
        "dynamic_symbols_not_in_scoring_candidate_set": bool(ev.get("dynamic_symbols_not_in_scoring_candidate_set")),
        "missing_from_scoring_candidate_set": bool(ev.get("missing_from_scoring_candidate_set")),
        "selected_entry_trace_seen": bool(ev.get("selected_entry_trace_seen")),
        "selected_entry_skip_reason": ev.get("selected_entry_skip_reason"),
        "reached_entry_eval": bool(row.get("reached_entry_eval")),
        "entry_eval_final": row.get("entry_eval_final"),
        "entry_eval_reject_reason": row.get("entry_eval_reject_reason"),
        "final_inferred_reason": _infer_reason(row, ev),
        "evidence_lines": list(ev.get("evidence_lines") or [])[:10],
    }


def _load_conversion_report(
    *,
    project_root: Path,
    data_dir: Path,
    day: str,
    user_id: str,
    log_paths: Sequence[Path | str] | None,
) -> tuple[dict[str, Any], str]:
    existing = _load_existing_conversion_artifact(data_dir, day=day, user_id=user_id)
    if existing is not None:
        return existing, "existing_dynamic_conversion_artifact"
    return (
        build_dynamic_conversion_report(
            project_root=project_root,
            data_dir=data_dir,
            day=day,
            user_id=user_id,
            log_paths=log_paths,
        ),
        "rebuilt_from_sources",
    )


def latest_dynamic_entry_gap_date(*, data_dir: Path | str = "data", user_id: str = "paper_bot") -> str | None:
    data = Path(data_dir)
    dates: set[str] = set()
    conversion_latest = latest_dynamic_conversion_date(data_dir=data, user_id=user_id)
    if conversion_latest:
        dates.add(conversion_latest)
    safe = _safe_user(user_id)
    for path in (data / "research" / "dynamic_conversion").glob(f"*_{safe}.json") if (data / "research" / "dynamic_conversion").exists() else []:
        day = _date_from_path(path)
        if day:
            dates.add(day)
    return sorted(dates)[-1] if dates else None


def build_dynamic_entry_gap_report(
    *,
    project_root: Path | str = ".",
    data_dir: Path | str = "data",
    day: str | date,
    user_id: str = "paper_bot",
    log_paths: Sequence[Path | str] | None = None,
    focus_symbols: Sequence[str] = _FOCUS_SYMBOLS,
) -> dict[str, Any]:
    """Build a read-only report for selected dynamic symbols lost before entry eval."""
    root = Path(project_root)
    data = Path(data_dir)
    day_s = (
        latest_dynamic_entry_gap_date(data_dir=data, user_id=user_id)
        if str(day).lower() == "latest"
        else day.isoformat()
        if isinstance(day, date)
        else str(day)
    )
    if not day_s:
        raise FileNotFoundError("No dynamic entry-gap date found.")
    conversion, conversion_source = _load_conversion_report(
        project_root=root,
        data_dir=data,
        day=day_s,
        user_id=user_id,
        log_paths=log_paths,
    )
    discovered_logs = _discover_log_paths(root, data, day=day_s, extra_paths=log_paths)
    evidence = _log_evidence(discovered_logs)
    symbols = conversion.get("symbols") if isinstance(conversion.get("symbols"), list) else []
    rows = [
        row
        for row in symbols
        if isinstance(row, Mapping)
        and int(row.get("selection_count") or 0) > 0
        and bool(row.get("appeared_in_dynamic_universe"))
    ]
    lost_rows = [row for row in rows if not bool(row.get("reached_entry_eval"))]
    gap_rows = sorted(
        [_gap_row(row, evidence) for row in lost_rows],
        key=lambda row: (str(row.get("symbol") or "") not in set(focus_symbols), str(row.get("symbol") or "")),
    )
    focus: dict[str, Any] = {}
    by_symbol = {str(row.get("symbol") or "").upper(): row for row in symbols if isinstance(row, Mapping)}
    for sym in focus_symbols:
        row = by_symbol.get(sym.upper())
        if row is None:
            focus[sym.upper()] = {
                "symbol": sym.upper(),
                "selected_count": 0,
                "entered_effective_universe": False,
                "reached_entry_eval": False,
                "final_inferred_reason": "not_observed",
            }
        else:
            focus[sym.upper()] = _gap_row(row, evidence)
    reason_counts = Counter(str(row.get("final_inferred_reason") or "unknown") for row in gap_rows)
    summary = {
        "selected_dynamic_symbols": len(
            [
                row
                for row in symbols
                if isinstance(row, Mapping) and int(row.get("selection_count") or 0) > 0
            ]
        ),
        "symbols_added_to_dynamic_universe": len(rows),
        "symbols_reaching_entry_eval": len(
            [
                row
                for row in symbols
                if isinstance(row, Mapping)
                and int(row.get("selection_count") or 0) > 0
                and bool(row.get("reached_entry_eval"))
            ]
        ),
        "symbols_lost_before_entry_eval": len(gap_rows),
        "drop_counts_by_inferred_reason": dict(sorted(reason_counts.items())),
    }
    recommendations = [
        "Add per-symbol research logging immediately after dynamic universe construction that records membership in _syms_scan and dynamic_set.",
        "Add a diagnostic-only log around momentum ranking with score, top_n, rank, and allowlist membership for each selected dynamic symbol.",
        "Add a diagnostic-only log around scoring_allowed top_n gating that states whether the dynamic bypass applied.",
        "Keep DYNAMIC_SELECTED_ENTRY_TRACE emitted before every possible pre-entry continue, including trend prefilter, history, cooldown, position, and routing gates.",
    ]
    return {
        "report": "dynamic_entry_gap",
        "research_only": True,
        "date": day_s,
        "user_id": user_id,
        "source_files": {
            "conversion_report_source": conversion_source,
            "conversion_report": str(data / "research" / "dynamic_conversion" / f"{day_s}_{_safe_user(user_id)}.json")
            if conversion_source == "existing_dynamic_conversion_artifact"
            else None,
            "logs": [str(path) for path in discovered_logs],
        },
        "summary": summary,
        "gap_symbols": gap_rows,
        "focus_symbols": focus,
        "instrumentation_recommendations": recommendations,
        "code_path_notes": [
            "src/app/live_cycle.py builds dynamic_set from DYNAMIC_UNIVERSE added/fastlane symbols.",
            "The entry loop iterates _syms_scan; a dynamic symbol can be in dynamic_set but still miss ENTRY_EVAL if it is absent from _syms_scan or hits a pre-entry continue.",
            "Observed pre-entry continues include momentum top_n/scoring gates, daily history guard, cooldown/position gates, and trend prefilter before entry decision logging.",
        ],
    }


def render_dynamic_entry_gap_report(report: Mapping[str, Any]) -> str:
    """Render a concise text report for operators."""
    summary = report.get("summary") if isinstance(report.get("summary"), Mapping) else {}
    lines = [
        f"Dynamic Entry Gap Report - {report.get('date')} user={report.get('user_id')}",
        "Research-only: no trading behavior, config, scanner thresholds, allocator behavior, or risk model changed.",
        "",
        "Summary",
        f"- selected_dynamic_symbols: {summary.get('selected_dynamic_symbols', 0)}",
        f"- symbols_added_to_dynamic_universe: {summary.get('symbols_added_to_dynamic_universe', 0)}",
        f"- symbols_reaching_entry_eval: {summary.get('symbols_reaching_entry_eval', 0)}",
        f"- symbols_lost_before_entry_eval: {summary.get('symbols_lost_before_entry_eval', 0)}",
        f"- drop_counts_by_inferred_reason: {summary.get('drop_counts_by_inferred_reason', {})}",
        "",
        "Focus Symbols",
    ]
    focus = report.get("focus_symbols") if isinstance(report.get("focus_symbols"), Mapping) else {}
    for sym in _FOCUS_SYMBOLS:
        row = focus.get(sym) if isinstance(focus.get(sym), Mapping) else {}
        lines.append(
            "- {symbol}: selected={selected} universe={universe} entry_eval={entry} "
            "top_n={top_n} trend_prefilter={trend} history={history} cooldown_position={cooldown} "
            "scoring_set_gap={scoring} reason={reason}".format(
                symbol=sym,
                selected=row.get("selected_count", 0),
                universe=row.get("entered_effective_universe", False),
                entry=row.get("reached_entry_eval", False),
                top_n=row.get("removed_by_top_n_scoring", False),
                trend=row.get("skipped_by_trend_prefilter", False),
                history=row.get("skipped_by_history_bars", False),
                cooldown=row.get("skipped_by_cooldown_or_position", False),
                scoring=row.get("dynamic_symbols_not_in_scoring_candidate_set", False),
                reason=row.get("final_inferred_reason"),
            )
        )
    lines.append("")
    lines.append("Lost Before ENTRY_EVAL")
    rows = report.get("gap_symbols") if isinstance(report.get("gap_symbols"), list) else []
    if not rows:
        lines.append("- none observed")
    for row in rows[:100]:
        if not isinstance(row, Mapping):
            continue
        lines.append(
            "- {symbol}: selected={selected} first={first} last={last} score={score} reason={reason}".format(
                symbol=row.get("symbol"),
                selected=row.get("selected_count"),
                first=row.get("first_selected_time"),
                last=row.get("last_selected_time"),
                score=row.get("dynamic_score"),
                reason=row.get("final_inferred_reason"),
            )
        )
    lines.append("")
    lines.append("Instrumentation Recommendations")
    for item in report.get("instrumentation_recommendations") or []:
        lines.append(f"- {item}")
    return "\n".join(lines) + "\n"


def dynamic_entry_gap_paths(*, data_dir: Path | str, user_id: str, day: str | date) -> DynamicEntryGapPaths:
    day_s = day.isoformat() if isinstance(day, date) else str(day)
    root = Path(data_dir) / "research" / "dynamic_entry_gap"
    stem = f"{day_s}_{_safe_user(user_id)}"
    return DynamicEntryGapPaths(json_path=root / f"{stem}.json", text_path=root / f"{stem}.txt")


def write_dynamic_entry_gap_report(
    *,
    project_root: Path | str = ".",
    data_dir: Path | str = "data",
    day: str | date,
    user_id: str = "paper_bot",
    log_paths: Sequence[Path | str] | None = None,
) -> tuple[Path, Path, dict[str, Any]]:
    report = build_dynamic_entry_gap_report(
        project_root=project_root,
        data_dir=data_dir,
        day=day,
        user_id=user_id,
        log_paths=log_paths,
    )
    paths = dynamic_entry_gap_paths(data_dir=data_dir, user_id=user_id, day=str(report["date"]))
    paths.json_path.parent.mkdir(parents=True, exist_ok=True)
    paths.json_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    paths.text_path.write_text(render_dynamic_entry_gap_report(report), encoding="utf-8")
    return paths.json_path, paths.text_path, report


__all__ = [
    "build_dynamic_entry_gap_report",
    "render_dynamic_entry_gap_report",
    "write_dynamic_entry_gap_report",
    "dynamic_entry_gap_paths",
    "latest_dynamic_entry_gap_date",
]
