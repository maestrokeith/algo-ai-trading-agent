"""Read-only dynamic scanner quality report."""

from __future__ import annotations

import gzip
import json
import math
import re
import subprocess
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Any

from src.config_loader import load_config
from src.dynamic_rvol_sensitivity import _history_paths_for_date
from src.exposure import ETF_SYMBOLS

_KV_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=([^ ]+)")
_ISO_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
_COMPACT_DATE_RE = re.compile(r"(\d{8})")
_ENTRY_PASS_RE = re.compile(r"\bENTRY_EVAL_PASS\s+symbol=([A-Z][A-Z0-9.\-]{0,12})\b")
_DEFAULT_LARGE_CAP_SYMBOLS = frozenset(
    {"AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "GOOG", "AVGO", "AMD", "ORCL", "NFLX", "TSLA", "JPM", "LLY"}
)


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


def _normalize_reason(value: Any) -> str:
    text = str(value or "").strip().lower().replace(" ", "_")
    if "unstable_quote" in text or "unstable quote" in text:
        return "unstable_quote"
    if "spread_too_wide" in text or "spread too wide" in text:
        return "spread_too_wide"
    if "below_min_price" in text or "min_price" in text:
        return "below_min_price"
    if "atr_expansion" in text:
        return "atr_expansion"
    if "entry_alignment" in text or "breakout" in text or "new_intraday_high" in text:
        return "entry_alignment"
    if "below_min_avg_volume" in text or "avg_volume" in text:
        return "below_min_avg_volume"
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


def _config_symbols(config: Mapping[str, Any]) -> set[str]:
    universe = config.get("universe") if isinstance(config.get("universe"), Mapping) else {}
    return {str(symbol or "").strip().upper() for symbol in universe.get("symbols") or [] if str(symbol or "").strip()}


def _large_cap_symbols(config: Mapping[str, Any]) -> set[str]:
    execution = config.get("execution") if isinstance(config.get("execution"), Mapping) else {}
    configured = {str(symbol or "").strip().upper() for symbol in execution.get("large_cap_symbols") or [] if str(symbol or "").strip()}
    return set(_DEFAULT_LARGE_CAP_SYMBOLS) | configured


def _is_right_or_warrant(symbol: str) -> bool:
    sym = str(symbol or "").strip().upper()
    return sym.endswith(".RT") or sym.endswith(".WS") or sym.endswith(".W") or sym.endswith("RT") or sym.endswith("WS")


def _symbol_type(symbol: str, *, core_symbols: set[str], large_caps: set[str]) -> str:
    sym = str(symbol or "").strip().upper()
    if _is_right_or_warrant(sym):
        return "rights/warrants/RT symbols"
    if sym in ETF_SYMBOLS or sym in core_symbols:
        return "ETF/core list"
    if sym in large_caps:
        return "large-cap"
    return "scanner-only"


def _quality_value(raw: Mapping[str, Any], key: str) -> Any:
    quality = raw.get("quality") if isinstance(raw.get("quality"), Mapping) else {}
    return raw.get(key, quality.get(key))


def _has_catalyst_values(row: Mapping[str, Any]) -> bool:
    return any((_safe_float(row.get(field)) or 0.0) > 0.0 for field in ("catalyst_score", "news_score", "event_score", "article_count"))


def _load_history_rows(
    *,
    data_dir: Path,
    day: str,
    user_id: str,
    history_dir: Path | None,
    core_symbols: set[str],
    large_caps: set[str],
) -> tuple[list[dict[str, Any]], str]:
    paths, source_mode = _history_files(data_dir, day=day, user_id=user_id, history_dir=history_dir)
    rows: list[dict[str, Any]] = []
    sequence = 0
    for path in paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(payload, Mapping):
            continue
        candidates = payload.get("candidates")
        if not isinstance(candidates, list):
            candidates = (payload.get("rejected") or []) + (payload.get("accepted") or []) + (payload.get("selected") or [])
        if not isinstance(candidates, list):
            continue
        for raw in candidates:
            if not isinstance(raw, Mapping) or _row_day(raw, payload, path) != day:
                continue
            symbol = str(raw.get("symbol") or "").strip().upper()
            if not symbol:
                continue
            sequence += 1
            reason = _normalize_reason(raw.get("rejection_reason") or raw.get("reason"))
            accepted = bool(raw.get("accepted"))
            rows.append(
                {
                    "symbol": symbol,
                    "timestamp": _candidate_timestamp(raw, payload),
                    "accepted": accepted,
                    "rejected": not accepted,
                    "rejection_reason": None if accepted else reason,
                    "price": _round(raw.get("price")),
                    "gain_pct": _round(raw.get("gain_pct", raw.get("day_gain_pct"))),
                    "volume": _round(raw.get("volume"), 2),
                    "relative_volume": _round(raw.get("relative_volume", raw.get("rel_volume"))),
                    "spread_pct": _round(raw.get("spread_pct")),
                    "atr_expansion_ratio": _round(_quality_value(raw, "atr_expansion_ratio")),
                    "catalyst_score": _round(raw.get("catalyst_score"), 4),
                    "news_score": _round(raw.get("news_score"), 2),
                    "event_score": _round(raw.get("event_score"), 2),
                    "article_count": _round(raw.get("article_count"), 2),
                    "symbol_type": _symbol_type(symbol, core_symbols=core_symbols, large_caps=large_caps),
                    "source_file": str(path),
                    "source_sequence": sequence,
                }
            )
    return rows, source_mode


def _entry_pass_symbols(lines: Sequence[str]) -> set[str]:
    symbols: set[str] = set()
    for line in lines:
        if "dynamic" not in line.lower() and "ENTRY_EVAL_PASS" not in line:
            continue
        match = _ENTRY_PASS_RE.search(line)
        if match:
            symbols.add(match.group(1).upper())
    return symbols


def _aggregate_symbol_rows(rows: Sequence[Mapping[str, Any]], *, entry_passed: set[str]) -> list[dict[str, Any]]:
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        if row.get("symbol_type") != "scanner-only":
            continue
        grouped.setdefault(str(row.get("symbol") or ""), []).append(row)
    out: list[dict[str, Any]] = []
    for symbol, symbol_rows in grouped.items():
        rejected = [row for row in symbol_rows if row.get("rejected")]
        reasons = Counter(str(row.get("rejection_reason") or "accepted") for row in symbol_rows if row.get("rejected"))
        spread_values = [_safe_float(row.get("spread_pct")) for row in symbol_rows]
        spreads = [value for value in spread_values if value is not None]
        accepted = any(bool(row.get("accepted")) for row in symbol_rows)
        row = {
            "symbol": symbol,
            "count_seen": len(symbol_rows),
            "count_rejected": len(rejected),
            "rejection_reasons": dict(reasons.most_common()),
            "max_gain_pct": _round(max((_safe_float(row.get("gain_pct")) or 0.0) for row in symbol_rows)),
            "max_volume": _round(max((_safe_float(row.get("volume")) or 0.0) for row in symbol_rows), 2),
            "max_RVOL": _round(max((_safe_float(row.get("relative_volume")) or 0.0) for row in symbol_rows)),
            "average_spread": round(sum(spreads) / len(spreads), 4) if spreads else None,
            "max_catalyst_score": _round(max((_safe_float(row.get("catalyst_score")) or 0.0) for row in symbol_rows), 4),
            "max_news_score": _round(max((_safe_float(row.get("news_score")) or 0.0) for row in symbol_rows), 2),
            "max_event_score": _round(max((_safe_float(row.get("event_score")) or 0.0) for row in symbol_rows), 2),
            "ever_accepted": accepted,
            "entry_eval_passed": symbol in entry_passed,
            "is_right_or_warrant": _is_right_or_warrant(symbol),
        }
        price = max((_safe_float(item.get("price")) or 0.0) for item in symbol_rows)
        row["safe_candidate_review"] = (
            row["count_rejected"] > 0
            and price >= 5.0
            and float(row["max_volume"] or 0.0) >= 100_000.0
            and float(row["average_spread"] or 999.0) <= 5.0
            and float(row["max_RVOL"] or 0.0) >= 1.0
            and (accepted or symbol in entry_passed)
        )
        out.append(row)
    return sorted(out, key=lambda row: (-int(row["count_seen"]), -int(row["count_rejected"]), str(row["symbol"])))


def _junk_pattern_groups(rows: Sequence[Mapping[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        if row.get("symbol_type") == "scanner-only":
            grouped.setdefault(str(row.get("symbol") or ""), []).append(row)
    out: dict[str, list[dict[str, Any]]] = {
        "no_catalyst_wide_spread": [],
        "no_catalyst_low_volume": [],
        "no_catalyst_below_min_price": [],
        "repeated_unstable_quote": [],
        "repeated_atr_expansion": [],
        "repeated_entry_alignment_failure": [],
    }
    for symbol, symbol_rows in grouped.items():
        reasons = Counter(str(row.get("rejection_reason") or "") for row in symbol_rows if row.get("rejected"))
        no_catalyst = all(not _has_catalyst_values(row) for row in symbol_rows)
        max_spread = max((_safe_float(row.get("spread_pct")) or 0.0) for row in symbol_rows)
        max_volume = max((_safe_float(row.get("volume")) or 0.0) for row in symbol_rows)
        base = {
            "symbol": symbol,
            "count": len(symbol_rows),
            "reasons": dict(reasons.most_common()),
            "max_volume": _round(max_volume, 2),
            "max_spread": _round(max_spread),
        }
        if no_catalyst and (max_spread > 5.0 or reasons.get("spread_too_wide", 0) > 0 or reasons.get("unstable_quote", 0) > 0):
            out["no_catalyst_wide_spread"].append(dict(base))
        if no_catalyst and (max_volume < 10_000.0 or reasons.get("below_min_avg_volume", 0) > 0):
            out["no_catalyst_low_volume"].append(dict(base))
        if no_catalyst and reasons.get("below_min_price", 0) > 0:
            out["no_catalyst_below_min_price"].append(dict(base))
        if reasons.get("unstable_quote", 0) >= 2:
            out["repeated_unstable_quote"].append(dict(base))
        if reasons.get("atr_expansion", 0) >= 2:
            out["repeated_atr_expansion"].append(dict(base))
        if reasons.get("entry_alignment", 0) >= 2:
            out["repeated_entry_alignment_failure"].append(dict(base))
    return {key: sorted(value, key=lambda row: (-int(row["count"]), str(row["symbol"]))) for key, value in out.items()}


def _recommendations(groups: Mapping[str, Sequence[Mapping[str, Any]]], symbol_rows: Sequence[Mapping[str, Any]]) -> list[str]:
    recs: list[str] = []
    if groups.get("no_catalyst_wide_spread"):
        recs.append("suppress scanner-only symbol for rest of day after N repeated wide-spread rejects")
    if any(bool(row.get("is_right_or_warrant")) for row in symbol_rows):
        recs.append("suppress .RT / warrant / rights symbols")
    if groups.get("no_catalyst_low_volume"):
        recs.append("require minimum liquidity for no-catalyst scanner-only names")
    if groups.get("no_catalyst_wide_spread"):
        recs.append("require catalyst for spread exception")
    if not recs:
        recs.append("no scanner-only suppression change recommended from this sample")
    return recs


def build_dynamic_scanner_quality_report(
    *,
    project_root: Path | str = ".",
    data_dir: Path | str = "data",
    day: date | str,
    user_id: str,
    history_dir: Path | str | None = None,
    log_text: str | None = None,
    log_files: Sequence[Path | str] | None = None,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a dynamic scanner quality report without trading side effects."""
    day_s = _day_text(day)
    root = Path(project_root)
    data = Path(data_dir)
    cfg: Mapping[str, Any] = config if config is not None else load_config(root / "config" / "default.yaml")
    core_symbols = _config_symbols(cfg)
    large_caps = _large_cap_symbols(cfg)
    history_path = Path(history_dir) if history_dir is not None else None
    rows, source_mode = _load_history_rows(
        data_dir=data,
        day=day_s,
        user_id=user_id,
        history_dir=history_path,
        core_symbols=core_symbols,
        large_caps=large_caps,
    )
    lines = _load_log_lines(project_root=root, data_dir=data, day=day_s, log_text=log_text, log_files=log_files)
    entry_passed = _entry_pass_symbols(lines)
    symbol_rows = _aggregate_symbol_rows(rows, entry_passed=entry_passed)
    repeated = [row for row in symbol_rows if int(row.get("count_seen") or 0) >= 2]
    groups = _junk_pattern_groups(rows)
    safe = [row for row in symbol_rows if row.get("safe_candidate_review")]
    return {
        "report": "dynamic_scanner_quality",
        "date": day_s,
        "user_id": str(user_id or "default"),
        "source": {
            "history_mode": source_mode,
            "log_line_count": len(lines),
            "scanner_only_symbol_count": len(symbol_rows),
        },
        "summary": {
            "repeated_scanner_only_symbols": len(repeated),
            "scanner_only_symbols": len(symbol_rows),
            "safe_candidate_review_count": len(safe),
        },
        "repeated_scanner_only_symbols": repeated,
        "junk_pattern_groups": groups,
        "candidate_suppression_recommendations": _recommendations(groups, symbol_rows),
        "safe_candidate_table": sorted(safe, key=lambda row: (-float(row.get("max_volume") or 0.0), str(row.get("symbol") or ""))),
    }


def _render_table(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    lines = [
        "| symbol | seen | rejected | reasons | max_gain_pct | max_volume | max_RVOL | average_spread | catalyst | news | event | ever_accepted | entry_eval_passed |",
        "| --- | ---: | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |",
    ]
    for row in rows:
        reasons = ", ".join(f"{key}:{value}" for key, value in dict(row.get("rejection_reasons") or {}).items())
        lines.append(
            f"| {row.get('symbol')} | {row.get('count_seen')} | {row.get('count_rejected')} | {reasons} | {row.get('max_gain_pct')} | {row.get('max_volume')} | {row.get('max_RVOL')} | {row.get('average_spread')} | {row.get('max_catalyst_score')} | {row.get('max_news_score')} | {row.get('max_event_score')} | {bool(row.get('ever_accepted'))} | {bool(row.get('entry_eval_passed'))} |"
        )
    return lines


def render_dynamic_scanner_quality_report(report: Mapping[str, Any]) -> str:
    """Render a dynamic scanner quality report as Markdown."""
    summary = report.get("summary") if isinstance(report.get("summary"), Mapping) else {}
    lines = [
        f"# Dynamic Scanner Quality Report {report.get('date')} user={report.get('user_id')}",
        "",
        "## Summary",
        f"- scanner-only symbols: {summary.get('scanner_only_symbols', 0)}",
        f"- repeated scanner-only symbols: {summary.get('repeated_scanner_only_symbols', 0)}",
        f"- safe candidate review count: {summary.get('safe_candidate_review_count', 0)}",
        "",
        "## Repeated Scanner-Only Symbols",
    ]
    lines.extend(_render_table(report.get("repeated_scanner_only_symbols") or []))
    lines.extend(["", "## Junk Pattern Groups"])
    groups = report.get("junk_pattern_groups") if isinstance(report.get("junk_pattern_groups"), Mapping) else {}
    for group, rows in groups.items():
        lines.append(f"### {group}")
        if not rows:
            lines.append("- none")
            continue
        for row in rows:
            reasons = ", ".join(f"{key}:{value}" for key, value in dict(row.get("reasons") or {}).items())
            lines.append(f"- {row.get('symbol')}: count={row.get('count')} reasons={reasons}")
    lines.extend(["", "## Candidate Suppression Recommendations"])
    for rec in report.get("candidate_suppression_recommendations") or []:
        lines.append(f"- {rec}")
    lines.extend(["", "## Safe Candidate Table"])
    lines.extend(_render_table(report.get("safe_candidate_table") or []))
    return "\n".join(lines).rstrip() + "\n"


def write_dynamic_scanner_quality_report(
    *,
    project_root: Path | str = ".",
    data_dir: Path | str = "data",
    day: date | str,
    user_id: str,
    history_dir: Path | str | None = None,
    log_text: str | None = None,
    log_files: Sequence[Path | str] | None = None,
    config: Mapping[str, Any] | None = None,
) -> tuple[Path, Path, dict[str, Any]]:
    """Build and write dynamic scanner quality JSON and Markdown artifacts."""
    data = Path(data_dir)
    day_s = _day_text(day)
    report = build_dynamic_scanner_quality_report(
        project_root=project_root,
        data_dir=data,
        day=day_s,
        user_id=user_id,
        history_dir=history_dir,
        log_text=log_text,
        log_files=log_files,
        config=config,
    )
    out_dir = data / "research_metrics" / day_s
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "dynamic_scanner_quality.json"
    text_path = out_dir / "dynamic_scanner_quality.md"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    text_path.write_text(render_dynamic_scanner_quality_report(report), encoding="utf-8")
    return json_path, text_path, report
