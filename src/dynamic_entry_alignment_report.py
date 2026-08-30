"""Read-only dynamic entry-alignment explainability report."""

from __future__ import annotations

import gzip
import json
import math
import re
import subprocess
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any

from src.dynamic_rvol_sensitivity import _history_paths_for_date

_KV_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=([^ ]+)")
_ISO_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
_COMPACT_DATE_RE = re.compile(r"(\d{8})")
_ENTRY_PASS_RE = re.compile(r"\bENTRY_EVAL_PASS\s+symbol=([A-Z][A-Z0-9.\-]{0,12})\b")
_CHECKS = (
    "above_vwap",
    "above_ema20",
    "above_ema50",
    "breakout",
    "higher_high",
    "strong_green_bar",
    "volume_confirmation",
    "momentum",
)
_REQUIRED_FEATURES = ("vwap", "ema20", "ema50", "atr", "five_min_trend", "momentum_score")


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


def _parse_kv(line: str) -> dict[str, str]:
    return {match.group(1): match.group(2).strip(",;") for match in _KV_RE.finditer(line)}


def _read_text(path: Path) -> str:
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    return path.read_text(encoding="utf-8", errors="replace")


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip().strip("$,;%")
    if text.lower() in {"", "none", "n/a", "nan", "null"}:
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
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "t", "yes", "y"}:
        return True
    if text in {"0", "false", "f", "no", "n"}:
        return False
    return None


def _parse_timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    match = re.search(r"\d{4}-\d{2}-\d{2}[T ][0-9:.:\-+Z]+", text)
    if match:
        text = match.group(0)
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _timestamp_from_line(line: str) -> str:
    text = line.strip()
    iso = re.search(r"\b\d{4}-\d{2}-\d{2}[T ][0-9:.:\-+Z]+\b", text)
    if iso:
        return iso.group(0)
    syslog = re.match(r"([A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})", text)
    if syslog:
        return syslog.group(1)
    return text[:24]


def _normalize_reason(value: Any) -> str:
    text = str(value or "").strip().lower().replace(" ", "_")
    if "entry_alignment" in text or "breakout" in text or "new_intraday_high" in text:
        return "entry_alignment"
    return re.sub(r"[^a-z0-9_]+", "_", text).strip("_") or "unknown"


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
            ["journalctl", "-u", service, "--since", f"{day} 09:00:00", "--until", f"{day} 23:59:59", "--no-pager"],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except Exception:
        return []
    return proc.stdout.splitlines() if proc.returncode == 0 else []


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


def _candidate_timestamp(raw: Mapping[str, Any], payload: Mapping[str, Any]) -> str | None:
    value = raw.get("timestamp") or raw.get("scan_timestamp") or raw.get("observed_at") or payload.get("generated_at")
    text = str(value or "").strip()
    return text or None


def _row_day(raw: Mapping[str, Any], payload: Mapping[str, Any], path: Path) -> str | None:
    ts = _candidate_timestamp(raw, payload) or str(payload.get("generated_at") or "")
    match = _ISO_DATE_RE.search(ts)
    if match:
        return match.group(1)
    return _date_from_path(path)


def _history_files(data_dir: Path, *, day: str, user_id: str, history_dir: Path | None = None) -> tuple[list[Path], str]:
    history_path = history_dir or data_dir / "dynamic_scan_history"
    return _history_paths_for_date(history_path, day=day, user_id=user_id)


def _quality(raw: Mapping[str, Any]) -> Mapping[str, Any]:
    value = raw.get("quality")
    return value if isinstance(value, Mapping) else {}


def _bool_from_reason(reason: str, key: str) -> bool | None:
    patterns = {
        "breakout": r"\bbreakout=(True|False|true|false|T|F)\b",
        "higher_high": r"\b(?:nh|higher_high|new_high)=(True|False|true|false|T|F)\b",
        "strong_green_bar": r"\b(?:green|strong_green|strong_green_bar)=(True|False|true|false|T|F)\b",
        "orb": r"\b(?:orb|opening_range_breakout)=(True|False|true|false|T|F)\b",
    }
    pattern = patterns.get(key)
    if not pattern:
        return None
    match = re.search(pattern, reason)
    return _parse_bool(match.group(1)) if match else None


def _first_bool(*values: Any) -> bool | None:
    for value in values:
        parsed = _parse_bool(value)
        if parsed is not None:
            return parsed
    return None


def _subchecks(raw: Mapping[str, Any], reason: str) -> dict[str, bool | None]:
    q = _quality(raw)
    above_vwap = _first_bool(raw.get("above_vwap"), raw.get("price_above_vwap"), q.get("price_above_vwap"))
    above_ema20 = _first_bool(raw.get("above_ema20"), raw.get("ema20_ok"), raw.get("price_above_ema20"))
    above_ema50 = _first_bool(raw.get("above_ema50"), raw.get("ema50_ok"), raw.get("price_above_ema50"))
    breakout = _first_bool(raw.get("breakout"), raw.get("breakout_ok"), _bool_from_reason(reason, "breakout"))
    higher_high = _first_bool(raw.get("higher_high"), raw.get("new_high"), _bool_from_reason(reason, "higher_high"))
    strong_green = _first_bool(raw.get("strong_green_bar"), raw.get("strong_green_1m"), _bool_from_reason(reason, "strong_green_bar"))
    orb = _first_bool(raw.get("orb"), raw.get("opening_range_breakout"), _bool_from_reason(reason, "orb"))
    volume_confirmation = _first_bool(raw.get("volume_confirmation"), raw.get("volume_confirmed"))
    if volume_confirmation is None:
        rvol = _safe_float(raw.get("relative_volume", raw.get("rel_volume")))
        volume_confirmation = rvol is not None and rvol >= 1.0
    momentum_score = _safe_float(raw.get("momentum_score") or raw.get("score"))
    momentum = None if momentum_score is None else momentum_score > 0.0
    return {
        "above_vwap": above_vwap,
        "above_ema20": above_ema20,
        "above_ema50": above_ema50,
        "breakout": breakout,
        "higher_high": higher_high,
        "strong_green_bar": strong_green,
        "volume_confirmation": volume_confirmation,
        "momentum": momentum,
        "orb": orb,
    }


def _failed_checks(checks: Mapping[str, bool | None]) -> list[str]:
    return [name for name in _CHECKS if checks.get(name) is False]


def _missing_features(row: Mapping[str, Any]) -> list[str]:
    missing: list[str] = []
    for key in _REQUIRED_FEATURES:
        value = row.get(key)
        if value is None or value == "":
            missing.append("trend_5m" if key == "five_min_trend" else key)
    return missing


def _classify_event(row: Mapping[str, Any]) -> str:
    return "data_quality_block" if _missing_features(row) else "strategy_rule"


def _flexible_setup_research(row: Mapping[str, Any]) -> dict[str, Any]:
    checks = row.get("alignment_subchecks") if isinstance(row.get("alignment_subchecks"), Mapping) else {}
    if _classify_event(row) == "data_quality_block":
        return {
            "strict_rejected": True,
            "flexible_would_pass": False,
            "setup_type": "feature_unavailable",
            "failed_rule": "feature_unavailable",
            "reason": "missing_required_features",
        }
    failed = [name for name in _CHECKS if checks.get(name) is False]
    above_vwap = checks.get("above_vwap") is True
    volume_ok = checks.get("volume_confirmation") is True
    momentum_ok = checks.get("momentum") is True
    trend = row.get("five_min_trend")
    trend_ok = bool(trend is True or str(trend).lower() in {"up", "true", "aligned"})
    setup = "none"
    would_pass = False
    if above_vwap and trend_ok and momentum_ok:
        setup = "vwap_reclaim"
        would_pass = len([name for name in failed if name not in {"higher_high", "strong_green_bar"}]) == 0
    elif trend_ok and volume_ok and momentum_ok:
        setup = "ema9_or_ema20_pullback"
        would_pass = len([name for name in failed if name not in {"higher_high", "strong_green_bar", "above_ema20"}]) == 0
    elif trend_ok and volume_ok:
        setup = "higher_low_continuation"
        would_pass = len([name for name in failed if name not in {"higher_high", "momentum"}]) == 0
    elif volume_ok and momentum_ok:
        setup = "consolidation_break"
        would_pass = len([name for name in failed if name not in {"higher_high", "strong_green_bar"}]) == 0
    return {
        "strict_rejected": True,
        "flexible_would_pass": bool(would_pass),
        "setup_type": setup,
        "failed_rule": failed[0] if len(failed) == 1 else ("multiple" if failed else "none"),
        "reason": "research_only_no_forward_data",
        "forward_return_5m": None,
        "forward_return_15m": None,
        "forward_return_30m": None,
        "mae_after_signal": None,
        "mfe_after_signal": None,
    }


def _entry_passes(lines: Sequence[str]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = defaultdict(list)
    for line in lines:
        match = _ENTRY_PASS_RE.search(line)
        if not match:
            continue
        out[match.group(1).upper()].append(_timestamp_from_line(line))
    return {symbol: sorted(times) for symbol, times in out.items()}


def _context_from_line(line: str) -> dict[str, Any] | None:
    if "ENTRY_ALIGNMENT_CONTEXT" not in line:
        return None
    kv = _parse_kv(line)
    symbol = str(kv.get("symbol") or "").strip().upper()
    if not symbol:
        return None
    checks = {
        "above_vwap": _parse_bool(kv.get("above_vwap")),
        "above_ema20": _parse_bool(kv.get("above_ema20")),
        "above_ema50": _parse_bool(kv.get("above_ema50")),
        "breakout": _parse_bool(kv.get("breakout")),
        "higher_high": _parse_bool(kv.get("higher_high")),
        "orb": _parse_bool(kv.get("orb")),
        "strong_green_bar": _parse_bool(kv.get("strong_green")),
        "volume_confirmation": _parse_bool(kv.get("volume_confirmation")),
    }
    momentum_score = _round(kv.get("momentum_score"))
    checks["momentum"] = None if momentum_score is None else momentum_score > 0.0
    event = {
        "symbol": symbol,
        "timestamp": _timestamp_from_line(line),
        "outcome": str(kv.get("outcome") or "").strip().lower(),
        "context_reason": kv.get("reason"),
        "price": _round(kv.get("price")),
        "gain_pct": _round(kv.get("gain_pct")),
        "RVOL": _round(kv.get("relative_volume")),
        "spread": _round(kv.get("spread_pct")),
        "vwap": _round(kv.get("vwap")),
        "vwap_distance": _round(kv.get("vwap_distance_pct")),
        "ema20": _round(kv.get("ema20")),
        "ema50": _round(kv.get("ema50")),
        "ema_distance_pct": _round(kv.get("ema_distance_pct")),
        "five_min_trend": kv.get("five_min_trend_direction"),
        "trend_strength": _round(kv.get("trend_strength")),
        "slope": _round(kv.get("slope")),
        "atr": _round(kv.get("atr")),
        "orb_status": checks.get("orb"),
        "breakout_state": checks.get("breakout"),
        "new_high_state": checks.get("higher_high"),
        "momentum_score": momentum_score,
        "alignment_subchecks": {name: checks.get(name) for name in _CHECKS},
        "failed_checks": _failed_checks(checks),
    }
    event["missing_features"] = _missing_features(event)
    event["classification"] = _classify_event(event)
    event["flexible_research"] = _flexible_setup_research(event)
    return event


def _entry_alignment_contexts(lines: Sequence[str]) -> list[dict[str, Any]]:
    contexts: list[dict[str, Any]] = []
    for line in lines:
        context = _context_from_line(line)
        if context is not None:
            contexts.append(context)
    return contexts


def _delay_minutes(start: str | None, later: str | None) -> float | None:
    start_dt = _parse_timestamp(start)
    later_dt = _parse_timestamp(later)
    if start_dt is None or later_dt is None or later_dt < start_dt:
        return None
    return round((later_dt - start_dt).total_seconds() / 60.0, 4)


def _event_from_row(raw: Mapping[str, Any], *, payload: Mapping[str, Any], path: Path, sequence: int) -> dict[str, Any] | None:
    reason_raw = str(raw.get("rejection_reason") or raw.get("reason") or "")
    if _normalize_reason(reason_raw) != "entry_alignment":
        return None
    symbol = str(raw.get("symbol") or "").strip().upper()
    if not symbol:
        return None
    checks = _subchecks(raw, reason_raw)
    q = _quality(raw)
    return {
        "symbol": symbol,
        "timestamp": _candidate_timestamp(raw, payload),
        "price": _round(raw.get("price")),
        "gain_pct": _round(raw.get("gain_pct", raw.get("day_gain_pct"))),
        "RVOL": _round(raw.get("relative_volume", raw.get("rel_volume"))),
        "spread": _round(raw.get("spread_pct")),
        "vwap": _round(raw.get("vwap")),
        "vwap_distance": _round(raw.get("vwap_distance_pct") or raw.get("vwap_distance")),
        "ema20": _round(raw.get("ema20")),
        "ema50": _round(raw.get("ema50")),
        "ema_distance_pct": _round(raw.get("ema_distance_pct")),
        "five_min_trend": q.get("five_min_trend_aligned"),
        "trend_strength": _round(raw.get("trend_strength")),
        "slope": _round(raw.get("slope")),
        "atr": _round(raw.get("atr")),
        "ema_relationship": raw.get("ema_relationship") or raw.get("ema_state"),
        "orb_status": checks.get("orb"),
        "breakout_state": checks.get("breakout"),
        "new_high_state": checks.get("higher_high"),
        "momentum_score": _round(raw.get("momentum_score") or raw.get("score")),
        "alignment_subchecks": {name: checks.get(name) for name in _CHECKS},
        "failed_checks": _failed_checks(checks),
        "raw_reason": reason_raw,
        "volume": _round(raw.get("volume"), 2),
        "passed_later": False,
        "passed_later_timestamp": None,
        "delay_until_alignment_passed_minutes": None,
        "source_file": str(path),
        "source_sequence": sequence,
    }
    event["missing_features"] = _missing_features(event)
    event["classification"] = _classify_event(event)
    event["flexible_research"] = _flexible_setup_research(event)
    return event


def _load_events(data_dir: Path, *, day: str, user_id: str, history_dir: Path | None) -> tuple[list[dict[str, Any]], str]:
    paths, source_mode = _history_files(data_dir, day=day, user_id=user_id, history_dir=history_dir)
    events: list[dict[str, Any]] = []
    sequence = 0
    for path in paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(payload, Mapping):
            continue
        rows = payload.get("candidates")
        if not isinstance(rows, list):
            rows = (payload.get("rejected") or []) + (payload.get("accepted") or []) + (payload.get("selected") or [])
        if not isinstance(rows, list):
            continue
        for raw in rows:
            if not isinstance(raw, Mapping) or _row_day(raw, payload, path) != day:
                continue
            sequence += 1
            event = _event_from_row(raw, payload=payload, path=path, sequence=sequence)
            if event is not None:
                events.append(event)
    return events, source_mode


def _context_for_event(event: Mapping[str, Any], contexts_by_symbol: Mapping[str, Sequence[Mapping[str, Any]]]) -> Mapping[str, Any] | None:
    symbol = str(event.get("symbol") or "").upper()
    candidates = [
        context
        for context in contexts_by_symbol.get(symbol, [])
        if str(context.get("outcome") or "").lower() != "pass"
    ]
    if not candidates:
        return None
    event_ts = _parse_timestamp(event.get("timestamp"))
    if event_ts is None:
        return candidates[0]
    parsed: list[tuple[float, Mapping[str, Any]]] = []
    for context in candidates:
        context_ts = _parse_timestamp(context.get("timestamp"))
        if context_ts is None:
            continue
        parsed.append((abs((context_ts - event_ts).total_seconds()), context))
    if not parsed:
        return candidates[0]
    parsed.sort(key=lambda row: row[0])
    return parsed[0][1]


def _apply_context(event: dict[str, Any], context: Mapping[str, Any]) -> None:
    for key in (
        "price",
        "gain_pct",
        "RVOL",
        "spread",
        "vwap",
        "vwap_distance",
        "ema20",
        "ema50",
        "ema_distance_pct",
        "five_min_trend",
        "trend_strength",
        "slope",
        "atr",
        "orb_status",
        "breakout_state",
        "new_high_state",
        "momentum_score",
    ):
        value = context.get(key)
        if value is not None and value != "":
            event[key] = value
    checks = event.get("alignment_subchecks") if isinstance(event.get("alignment_subchecks"), Mapping) else {}
    merged_checks = dict(checks)
    context_checks = context.get("alignment_subchecks") if isinstance(context.get("alignment_subchecks"), Mapping) else {}
    for name in _CHECKS:
        if context_checks.get(name) is not None:
            merged_checks[name] = context_checks.get(name)
    event["alignment_subchecks"] = merged_checks
    event["failed_checks"] = _failed_checks(merged_checks)
    event["context_timestamp"] = context.get("timestamp")
    if context.get("context_reason"):
        event["context_reason"] = context.get("context_reason")
    event["missing_features"] = _missing_features(event)
    event["classification"] = _classify_event(event)
    event["flexible_research"] = _flexible_setup_research(event)


def _mean(values: Sequence[float]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def _metric_average(rows: Sequence[Mapping[str, Any]], key: str) -> float | None:
    values = [float(value) for row in rows if (value := _safe_float(row.get(key))) is not None]
    return _mean(values)


def _context_average(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "count": len(rows),
        "momentum_avg": _metric_average(rows, "momentum_score"),
        "vwap_distance_avg": _metric_average(rows, "vwap_distance"),
        "ema_distance_avg": _metric_average(rows, "ema_distance_pct"),
    }


def _context_average_diff(pass_avg: Mapping[str, Any], fail_avg: Mapping[str, Any]) -> dict[str, float | None]:
    diff: dict[str, float | None] = {}
    for key in ("momentum_avg", "vwap_distance_avg", "ema_distance_avg"):
        p = _safe_float(pass_avg.get(key))
        f = _safe_float(fail_avg.get(key))
        diff[key] = round(p - f, 4) if p is not None and f is not None else None
    return diff


def build_dynamic_entry_alignment_report(
    *,
    project_root: Path | str = ".",
    data_dir: Path | str = "data",
    day: date | str,
    user_id: str,
    history_dir: Path | str | None = None,
    log_text: str | None = None,
    log_files: Sequence[Path | str] | None = None,
) -> dict[str, Any]:
    """Build a read-only entry-alignment explainability report."""
    day_s = _day_text(day)
    root = Path(project_root)
    data = Path(data_dir)
    history_path = Path(history_dir) if history_dir is not None else None
    events, source_mode = _load_events(data, day=day_s, user_id=user_id, history_dir=history_path)
    lines = _load_log_lines(project_root=root, data_dir=data, day=day_s, log_text=log_text, log_files=log_files)
    passes = _entry_passes(lines)
    contexts = _entry_alignment_contexts(lines)
    contexts_by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for context in contexts:
        contexts_by_symbol[str(context.get("symbol") or "").upper()].append(context)
    for event in events:
        context = _context_for_event(event, contexts_by_symbol)
        if context is not None:
            _apply_context(event, context)

    failure_counts: Counter[str] = Counter()
    classification_counts: Counter[str] = Counter()
    missing_feature_counts: Counter[str] = Counter()
    flexible_setup_counts: Counter[str] = Counter()
    by_symbol_failure: dict[str, Counter[str]] = defaultdict(Counter)
    for event in events:
        event["missing_features"] = _missing_features(event)
        event["classification"] = _classify_event(event)
        event["flexible_research"] = _flexible_setup_research(event)
        classification_counts[str(event.get("classification") or "unknown")] += 1
        for feature in event.get("missing_features") or []:
            missing_feature_counts[str(feature)] += 1
        research = event.get("flexible_research") if isinstance(event.get("flexible_research"), Mapping) else {}
        flexible_setup_counts[str(research.get("setup_type") or "none")] += 1
        symbol = str(event.get("symbol") or "")
        if event.get("classification") == "data_quality_block":
            failure_counts["feature_unavailable"] += 1
            by_symbol_failure[symbol]["feature_unavailable"] += 1
        for check in ([] if event.get("classification") == "data_quality_block" else event.get("failed_checks") or []):
            failure_counts[str(check)] += 1
            by_symbol_failure[symbol][str(check)] += 1
        later_times = [ts for ts in passes.get(symbol, []) if _delay_minutes(str(event.get("timestamp") or ""), ts) is not None]
        if later_times:
            first_later = later_times[0]
            event["passed_later"] = True
            event["passed_later_timestamp"] = first_later
            event["delay_until_alignment_passed_minutes"] = _delay_minutes(str(event.get("timestamp") or ""), first_later)

    delays = [
        float(value)
        for event in events
        if (value := _safe_float(event.get("delay_until_alignment_passed_minutes"))) is not None
    ]
    repeated_failures = [
        {"symbol": symbol, "sub_check": check, "count": count}
        for symbol, counts in by_symbol_failure.items()
        for check, count in counts.items()
        if count >= 2
    ]
    repeated_failures.sort(key=lambda row: (-int(row["count"]), str(row["symbol"]), str(row["sub_check"])))
    high_liquidity = [event for event in events if (_safe_float(event.get("volume")) or 0.0) > 1_000_000.0]
    passed_later = [event for event in events if event.get("passed_later")]
    entry_scores = [
        float(value)
        for event in events
        if (value := _safe_float(event.get("entry_quality_score"))) is not None
    ]
    score_hist = Counter(str(int(score // 10 * 10)) for score in entry_scores)
    adaptive_reason_counts = Counter(
        str(event.get("entry_quality_reason"))
        for event in events
        if event.get("entry_quality_reason")
    )
    pass_contexts = [context for context in contexts if str(context.get("outcome") or "").lower() == "pass"]
    fail_contexts = [context for context in contexts if str(context.get("outcome") or "").lower() == "fail"]
    pass_avg = _context_average(pass_contexts)
    fail_avg = _context_average(fail_contexts or events)
    return {
        "report": "dynamic_entry_alignment",
        "date": day_s,
        "user_id": str(user_id or "default"),
        "source": {
            "history_mode": source_mode,
            "log_line_count": len(lines),
            "event_count": len(events),
            "entry_alignment_context_count": len(contexts),
        },
        "summary": {
            "entry_alignment_rejections": len(events),
            "failure_counts_by_sub_check": dict(failure_counts.most_common()),
            "classification_counts": dict(classification_counts.most_common()),
            "missing_feature_counts": dict(missing_feature_counts.most_common()),
            "flexible_research_setup_counts": dict(flexible_setup_counts.most_common()),
            "entry_quality_adaptive_scoring": {
                "average_entry_score": _mean(entry_scores),
                "score_histogram": dict(score_hist),
                "reasons_removed_by_adaptive_scoring": dict(adaptive_reason_counts),
                "adaptive_entries": sum(1 for event in events if bool(event.get("adaptive_entry"))),
                "adaptive_entries_pnl": None,
            },
            "symbols_repeatedly_failing_same_sub_check": repeated_failures,
            "high_liquidity_candidate_count": len(high_liquidity),
            "candidates_passed_later_count": len(passed_later),
            "average_delay_until_alignment_passed_minutes": round(sum(delays) / len(delays), 4) if delays else None,
            "alignment_context_averages": {
                "pass": pass_avg,
                "fail": fail_avg,
                "diff": _context_average_diff(pass_avg, fail_avg),
            },
        },
        "high_liquidity_candidates": high_liquidity,
        "candidates_passed_later": passed_later,
        "events": sorted(events, key=lambda row: (str(row.get("timestamp") or ""), str(row.get("symbol") or ""))),
    }


def _check_mark(value: bool | None) -> str:
    if value is True:
        return "✓"
    if value is False:
        return "✗"
    return "?"


def render_dynamic_entry_alignment_report(report: Mapping[str, Any]) -> str:
    """Render the dynamic entry-alignment report as Markdown."""
    summary = report.get("summary") if isinstance(report.get("summary"), Mapping) else {}
    lines = [
        f"# Dynamic Entry Alignment Report {report.get('date')} user={report.get('user_id')}",
        "",
        "## Summary",
        f"- entry_alignment rejections: {summary.get('entry_alignment_rejections', 0)}",
        f"- high-liquidity candidates: {summary.get('high_liquidity_candidate_count', 0)}",
        f"- candidates passed later: {summary.get('candidates_passed_later_count', 0)}",
        f"- average delay until alignment passed: {summary.get('average_delay_until_alignment_passed_minutes')}",
        "",
        "## Alignment Context Averages",
        "| Metric | PASS | FAIL | PASS-FAIL |",
        "| --- | ---: | ---: | ---: |",
    ]
    averages = summary.get("alignment_context_averages") if isinstance(summary.get("alignment_context_averages"), Mapping) else {}
    pass_avg = averages.get("pass") if isinstance(averages.get("pass"), Mapping) else {}
    fail_avg = averages.get("fail") if isinstance(averages.get("fail"), Mapping) else {}
    diff_avg = averages.get("diff") if isinstance(averages.get("diff"), Mapping) else {}
    for label, key in (
        ("momentum avg", "momentum_avg"),
        ("VWAP distance avg", "vwap_distance_avg"),
        ("EMA distance avg", "ema_distance_avg"),
    ):
        lines.append(f"| {label} | {pass_avg.get(key)} | {fail_avg.get(key)} | {diff_avg.get(key)} |")
    lines.extend(["", "## Failure Counts By Sub-Check"])
    for check, count in dict(summary.get("failure_counts_by_sub_check") or {}).items():
        lines.append(f"- {check}: {count}")
    lines.extend(["", "## Classification Counts"])
    for classification, count in dict(summary.get("classification_counts") or {}).items():
        lines.append(f"- {classification}: {count}")
    lines.extend(["", "## Missing Feature Counts"])
    for feature, count in dict(summary.get("missing_feature_counts") or {}).items():
        lines.append(f"- {feature}: {count}")
    lines.extend(["", "## Flexible Research Setup Counts"])
    for setup, count in dict(summary.get("flexible_research_setup_counts") or {}).items():
        lines.append(f"- {setup}: {count}")
    adaptive_quality = summary.get("entry_quality_adaptive_scoring") if isinstance(summary.get("entry_quality_adaptive_scoring"), Mapping) else {}
    lines.extend(["", "## Entry Quality Adaptive Scoring"])
    lines.append(f"- average entry score: {adaptive_quality.get('average_entry_score')}")
    lines.append(f"- score histogram: {adaptive_quality.get('score_histogram') or {}}")
    lines.append(f"- reasons removed by adaptive scoring: {adaptive_quality.get('reasons_removed_by_adaptive_scoring') or {}}")
    lines.append(f"- adaptive entries: {adaptive_quality.get('adaptive_entries')}")
    lines.append(f"- adaptive entries pnl: {adaptive_quality.get('adaptive_entries_pnl')}")
    lines.extend(["", "## Symbols Repeatedly Failing Same Sub-Check"])
    repeated = summary.get("symbols_repeatedly_failing_same_sub_check") or []
    if not repeated:
        lines.append("- none")
    for row in repeated:
        lines.append(f"- {row.get('symbol')}: {row.get('sub_check')} x{row.get('count')}")
    lines.extend(["", "## Rejections"])
    for event in report.get("events") or []:
        lines.extend(
            [
                f"### {event.get('symbol')} {event.get('timestamp')}",
                f"- price: {event.get('price')} gain_pct: {event.get('gain_pct')} RVOL: {event.get('RVOL')} spread: {event.get('spread')}",
                f"- VWAP: {event.get('vwap')} VWAP distance: {event.get('vwap_distance')} 5m trend: {event.get('five_min_trend')} trend strength: {event.get('trend_strength')} slope: {event.get('slope')}",
                f"- EMA20: {event.get('ema20')} EMA50: {event.get('ema50')} EMA distance: {event.get('ema_distance_pct')} EMA: {event.get('ema_relationship')} ATR: {event.get('atr')}",
                f"- ORB: {event.get('orb_status')} breakout: {event.get('breakout_state')} new high: {event.get('new_high_state')} momentum: {event.get('momentum_score')}",
                f"- classification: {event.get('classification')} missing_features: {','.join(event.get('missing_features') or []) or 'none'}",
                f"- flexible research: {event.get('flexible_research')}",
                "- FAILED CHECKS:",
            ]
        )
        checks = event.get("alignment_subchecks") if isinstance(event.get("alignment_subchecks"), Mapping) else {}
        for check in _CHECKS:
            lines.append(f"  - {_check_mark(checks.get(check))} {check}")
        lines.append(f"- raw reason: {event.get('raw_reason')}")
        lines.append(f"- passed later: {bool(event.get('passed_later'))} delay_min: {event.get('delay_until_alignment_passed_minutes')}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_dynamic_entry_alignment_report(
    *,
    project_root: Path | str = ".",
    data_dir: Path | str = "data",
    day: date | str,
    user_id: str,
    history_dir: Path | str | None = None,
    log_text: str | None = None,
    log_files: Sequence[Path | str] | None = None,
) -> tuple[Path, Path, dict[str, Any]]:
    """Build and write dynamic entry-alignment report artifacts."""
    data = Path(data_dir)
    day_s = _day_text(day)
    report = build_dynamic_entry_alignment_report(
        project_root=project_root,
        data_dir=data,
        day=day_s,
        user_id=user_id,
        history_dir=history_dir,
        log_text=log_text,
        log_files=log_files,
    )
    out_dir = data / "research_metrics" / day_s
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "dynamic_entry_alignment.json"
    text_path = out_dir / "dynamic_entry_alignment.md"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    text_path.write_text(render_dynamic_entry_alignment_report(report), encoding="utf-8")
    return json_path, text_path, report
