"""Read-only dynamic spread-too-wide rejection tier research."""

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
_SCAN_REJECT_RE = re.compile(r"\bDYNAMIC_SCAN reject\s+(?P<symbol>[A-Z][A-Z0-9.\-]{0,12}):\s*(?P<body>.+)$")
_DEFAULT_LARGE_CAP_SYMBOLS = frozenset(
    {
        "AAPL",
        "MSFT",
        "NVDA",
        "AMZN",
        "META",
        "GOOGL",
        "GOOG",
        "AVGO",
        "AMD",
        "ORCL",
        "NFLX",
        "TSLA",
        "JPM",
        "LLY",
        "V",
        "MA",
        "COST",
        "WMT",
    }
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
    return re.sub(r"[^a-z0-9_]+", "_", text).strip("_")


def _timestamp_from_line(line: str) -> str:
    text = line.strip()
    iso = re.search(r"\b\d{4}-\d{2}-\d{2}[T ][0-9:.:\-+Z]+\b", text)
    if iso:
        return iso.group(0)
    syslog = re.match(r"([A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})", text)
    if syslog:
        return syslog.group(1)
    return text[:24]


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
            [
                "journalctl",
                "-u",
                service,
                "--since",
                f"{day} 09:00:00",
                "--until",
                f"{day} 23:59:59",
                "--no-pager",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except Exception:
        return []
    if proc.returncode != 0:
        return []
    return proc.stdout.splitlines()


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


def _price_bucket(price: float | None) -> str:
    if price is None:
        return "unknown"
    if price < 1:
        return "<$1"
    if price < 5:
        return "$1-$5"
    if price < 20:
        return "$5-$20"
    if price <= 100:
        return "$20-$100"
    return ">$100"


def _volume_bucket(volume: float | None) -> str:
    if volume is None:
        return "unknown"
    if volume <= 0:
        return "zero"
    if volume < 10_000:
        return "<10k"
    if volume < 100_000:
        return "10k-100k"
    if volume < 1_000_000:
        return "100k-1M"
    return ">1M"


def _is_right_or_warrant(symbol: str) -> bool:
    sym = str(symbol or "").strip().upper()
    return (
        sym.endswith(".RT")
        or sym.endswith(".WS")
        or sym.endswith(".W")
        or sym.endswith("WS")
        or sym.endswith("W")
        or sym.endswith("RT")
        or "WARRANT" in sym
        or "RIGHT" in sym
    )


def _config_symbols(config: Mapping[str, Any]) -> set[str]:
    universe = config.get("universe") if isinstance(config.get("universe"), Mapping) else {}
    return {str(symbol or "").strip().upper() for symbol in universe.get("symbols") or [] if str(symbol or "").strip()}


def _large_cap_symbols(config: Mapping[str, Any]) -> set[str]:
    execution = config.get("execution") if isinstance(config.get("execution"), Mapping) else {}
    configured = {str(symbol or "").strip().upper() for symbol in execution.get("large_cap_symbols") or [] if str(symbol or "").strip()}
    return set(_DEFAULT_LARGE_CAP_SYMBOLS) | configured


def _symbol_type(symbol: str, *, core_symbols: set[str], large_caps: set[str]) -> str:
    sym = str(symbol or "").strip().upper()
    if _is_right_or_warrant(sym):
        return "rights/warrants/RT symbols"
    if sym in ETF_SYMBOLS or sym in core_symbols:
        return "ETF/core list"
    if sym in large_caps:
        return "large-cap"
    return "scanner-only"


def _has_catalyst(row: Mapping[str, Any]) -> bool:
    return any(
        (_safe_float(row.get(field)) or 0.0) > 0.0
        for field in ("catalyst_score", "news_score", "event_score", "article_count")
    )


def _future_exception_candidate(row: Mapping[str, Any]) -> bool:
    symbol = str(row.get("symbol") or "").strip().upper()
    price = _safe_float(row.get("price"))
    volume = _safe_float(row.get("volume"))
    spread = _safe_float(row.get("spread_pct"))
    catalyst_score = _safe_float(row.get("catalyst_score")) or 0.0
    news_score = _safe_float(row.get("news_score")) or 0.0
    event_score = _safe_float(row.get("event_score")) or 0.0
    return (
        price is not None
        and price >= 20.0
        and volume is not None
        and volume >= 500_000.0
        and spread is not None
        and spread <= 5.0
        and (catalyst_score >= 0.7 or news_score >= 7.0 or event_score >= 7.0)
        and not _is_right_or_warrant(symbol)
        and volume > 0.0
    )


def _row_from_history(
    raw: Mapping[str, Any],
    *,
    payload: Mapping[str, Any],
    path: Path,
    sequence: int,
    core_symbols: set[str],
    large_caps: set[str],
) -> dict[str, Any] | None:
    reason = _normalize_reason(raw.get("rejection_reason") or raw.get("reason"))
    if reason != "spread_too_wide":
        return None
    symbol = str(raw.get("symbol") or "").strip().upper()
    if not symbol:
        return None
    price = _safe_float(raw.get("price"))
    volume = _safe_float(raw.get("volume"))
    spread_pct = _safe_float(raw.get("spread_pct") or raw.get("spread"))
    row = {
        "symbol": symbol,
        "timestamp": _candidate_timestamp(raw, payload),
        "price": _round(price),
        "volume": _round(volume, 2),
        "spread_pct": _round(spread_pct),
        "gain_pct": _round(raw.get("gain_pct", raw.get("day_gain_pct"))),
        "relative_volume": _round(raw.get("relative_volume", raw.get("rel_volume"))),
        "news_score": _round(raw.get("news_score"), 2),
        "catalyst_score": _round(raw.get("catalyst_score"), 4),
        "event_score": _round(raw.get("event_score"), 2),
        "article_count": _round(raw.get("article_count"), 2),
        "price_bucket": _price_bucket(price),
        "volume_bucket": _volume_bucket(volume),
        "symbol_type": _symbol_type(symbol, core_symbols=core_symbols, large_caps=large_caps),
        "catalyst_bucket": "catalyst" if _has_catalyst(raw) else "no_catalyst",
        "source": "dynamic_scan_history",
        "source_file": str(path),
        "source_sequence": sequence,
    }
    row["future_safe_exception_candidate"] = _future_exception_candidate(row)
    return row


def _rows_from_history(
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
            candidates = payload.get("rejected") or []
        if not isinstance(candidates, list):
            continue
        for raw in candidates:
            if not isinstance(raw, Mapping):
                continue
            if _row_day(raw, payload, path) != day:
                continue
            sequence += 1
            row = _row_from_history(
                raw,
                payload=payload,
                path=path,
                sequence=sequence,
                core_symbols=core_symbols,
                large_caps=large_caps,
            )
            if row is not None:
                rows.append(row)
    return rows, source_mode


def _row_from_log_line(
    line: str,
    *,
    core_symbols: set[str],
    large_caps: set[str],
) -> dict[str, Any] | None:
    match = _SCAN_REJECT_RE.search(line)
    if not match:
        return None
    body = match.group("body").lower()
    if "spread too wide" not in body and "spread_too_wide" not in body:
        return None
    kv = _parse_kv(line)
    symbol = match.group("symbol").upper()
    price = _safe_float(kv.get("price"))
    volume = _safe_float(kv.get("volume"))
    spread_pct = _safe_float(kv.get("spread_pct") or kv.get("spread"))
    catalyst_score = _safe_float(kv.get("catalyst_score"))
    news_score = _safe_float(kv.get("news_score"))
    event_score = _safe_float(kv.get("event_score"))
    article_count = _safe_float(kv.get("article_count"))
    row = {
        "symbol": symbol,
        "timestamp": _timestamp_from_line(line),
        "price": _round(price),
        "volume": _round(volume, 2),
        "spread_pct": _round(spread_pct),
        "gain_pct": _round(kv.get("gain_pct")),
        "relative_volume": _round(kv.get("relative_volume") or kv.get("rel_volume")),
        "news_score": _round(news_score, 2),
        "catalyst_score": _round(catalyst_score, 4),
        "event_score": _round(event_score, 2),
        "article_count": _round(article_count, 2),
        "price_bucket": _price_bucket(price),
        "volume_bucket": _volume_bucket(volume),
        "symbol_type": _symbol_type(symbol, core_symbols=core_symbols, large_caps=large_caps),
        "catalyst_bucket": "catalyst"
        if any((value or 0.0) > 0.0 for value in (catalyst_score, news_score, event_score, article_count))
        else "no_catalyst",
        "source": "logs",
        "source_file": None,
        "source_sequence": None,
    }
    row["future_safe_exception_candidate"] = _future_exception_candidate(row)
    return row


def _dedupe_key(row: Mapping[str, Any]) -> tuple[str, str, str, str, str]:
    return (
        str(row.get("timestamp") or ""),
        str(row.get("symbol") or ""),
        str(row.get("price") or ""),
        str(row.get("volume") or ""),
        str(row.get("spread_pct") or ""),
    )


def _counter(rows: Sequence[Mapping[str, Any]], key: str) -> dict[str, int]:
    return dict(Counter(str(row.get(key) or "unknown") for row in rows).most_common())


def _top_liquid(rows: Sequence[Mapping[str, Any]], limit: int = 20) -> list[dict[str, Any]]:
    return sorted(
        (dict(row) for row in rows if (_safe_float(row.get("volume")) or 0.0) > 0.0),
        key=lambda row: (
            -float(_safe_float(row.get("volume")) or 0.0),
            float(_safe_float(row.get("spread_pct")) or 999.0),
            str(row.get("symbol") or ""),
        ),
    )[:limit]


def _repeated_junk(rows: Sequence[Mapping[str, Any]], limit: int = 20) -> list[dict[str, Any]]:
    by_symbol: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        symbol = str(row.get("symbol") or "")
        by_symbol.setdefault(symbol, []).append(row)
    out: list[dict[str, Any]] = []
    for symbol, symbol_rows in by_symbol.items():
        zero = sum(1 for row in symbol_rows if (_safe_float(row.get("volume")) or 0.0) <= 0.0)
        right = 1 if _is_right_or_warrant(symbol) else 0
        avg_spread_values = [_safe_float(row.get("spread_pct")) for row in symbol_rows]
        avg_spread_clean = [value for value in avg_spread_values if value is not None]
        max_volume = max((_safe_float(row.get("volume")) or 0.0) for row in symbol_rows)
        out.append(
            {
                "symbol": symbol,
                "count": len(symbol_rows),
                "zero_volume_count": zero,
                "symbol_type": str(symbol_rows[0].get("symbol_type") or "unknown"),
                "average_spread_pct": round(sum(avg_spread_clean) / len(avg_spread_clean), 4)
                if avg_spread_clean
                else None,
                "max_volume": _round(max_volume, 2),
                "junk_score": len(symbol_rows) + zero * 2 + right * 5,
            }
        )
    return sorted(out, key=lambda row: (-int(row["junk_score"]), -int(row["count"]), str(row["symbol"])))[:limit]


def build_dynamic_spread_tier_research_report(
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
    """Build spread-too-wide tier research without changing trading behavior."""
    day_s = _day_text(day)
    root = Path(project_root)
    data = Path(data_dir)
    cfg: Mapping[str, Any] = config if config is not None else load_config(root / "config" / "default.yaml")
    core_symbols = _config_symbols(cfg)
    large_caps = _large_cap_symbols(cfg)
    history_path = Path(history_dir) if history_dir is not None else None
    rows, source_mode = _rows_from_history(
        data_dir=data,
        day=day_s,
        user_id=user_id,
        history_dir=history_path,
        core_symbols=core_symbols,
        large_caps=large_caps,
    )
    lines = _load_log_lines(project_root=root, data_dir=data, day=day_s, log_text=log_text, log_files=log_files)
    rows.extend(
        row
        for row in (_row_from_log_line(line, core_symbols=core_symbols, large_caps=large_caps) for line in lines)
        if row is not None
    )

    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    for row in rows:
        key = _dedupe_key(row)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)

    exception_candidates = sorted(
        [dict(row) for row in deduped if row.get("future_safe_exception_candidate")],
        key=lambda row: (
            float(_safe_float(row.get("spread_pct")) or 999.0),
            -float(_safe_float(row.get("volume")) or 0.0),
            str(row.get("symbol") or ""),
        ),
    )

    return {
        "report": "dynamic_spread_tier_research",
        "date": day_s,
        "user_id": str(user_id or "default"),
        "source": {
            "history_mode": source_mode,
            "log_line_count": len(lines),
            "event_count": len(deduped),
        },
        "summary": {
            "total_spread_too_wide_rejects": len(deduped),
            "price_buckets": _counter(deduped, "price_bucket"),
            "volume_buckets": _counter(deduped, "volume_bucket"),
            "symbol_types": _counter(deduped, "symbol_type"),
            "catalyst_vs_no_catalyst": _counter(deduped, "catalyst_bucket"),
            "future_safe_exception_candidate_count": len(exception_candidates),
        },
        "top_liquid_rejected_candidates": _top_liquid(deduped),
        "top_repeated_junk_symbols": _repeated_junk(deduped),
        "future_safe_exception_candidates": exception_candidates,
        "events": sorted(
            deduped,
            key=lambda row: (str(row.get("timestamp") or ""), str(row.get("symbol") or ""), str(row.get("source") or "")),
        ),
    }


def _render_counter(title: str, payload: Mapping[str, Any]) -> list[str]:
    lines = [title]
    if not payload:
        lines.append("- none")
        return lines
    for key, value in payload.items():
        lines.append(f"- {key}: {value}")
    return lines


def _render_rows(rows: Sequence[Mapping[str, Any]], *, include_candidate_flag: bool = False) -> list[str]:
    header = (
        "| symbol | count | price | volume | spread_pct | catalyst_score | news_score | event_score | type | timestamp |"
        if not include_candidate_flag
        else "| symbol | price | volume | spread_pct | catalyst_score | news_score | event_score | type | qualifies | timestamp |"
    )
    lines = [header]
    lines.append(
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |"
        if not include_candidate_flag
        else "| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- | --- |"
    )
    for row in rows:
        if "count" in row and not include_candidate_flag:
            lines.append(
                f"| {row.get('symbol')} | {row.get('count')} |  | {row.get('max_volume')} | {row.get('average_spread_pct')} |  |  |  | {row.get('symbol_type')} |  |"
            )
        elif include_candidate_flag:
            lines.append(
                f"| {row.get('symbol')} | {row.get('price')} | {row.get('volume')} | {row.get('spread_pct')} | {row.get('catalyst_score')} | {row.get('news_score')} | {row.get('event_score')} | {row.get('symbol_type')} | {bool(row.get('future_safe_exception_candidate'))} | {row.get('timestamp') or ''} |"
            )
        else:
            lines.append(
                f"| {row.get('symbol')} |  | {row.get('price')} | {row.get('volume')} | {row.get('spread_pct')} | {row.get('catalyst_score')} | {row.get('news_score')} | {row.get('event_score')} | {row.get('symbol_type')} | {row.get('timestamp') or ''} |"
            )
    return lines


def render_dynamic_spread_tier_research_report(report: Mapping[str, Any]) -> str:
    """Render spread-tier research as Markdown."""
    summary = report.get("summary") if isinstance(report.get("summary"), Mapping) else {}
    lines = [
        f"# Dynamic Spread Tier Research {report.get('date')} user={report.get('user_id')}",
        "",
        "## Summary",
        f"- total spread_too_wide rejects: {summary.get('total_spread_too_wide_rejects', 0)}",
        f"- future safe exception candidates: {summary.get('future_safe_exception_candidate_count', 0)}",
        "",
    ]
    lines.extend(_render_counter("## Price Buckets", summary.get("price_buckets") or {}))
    lines.append("")
    lines.extend(_render_counter("## Volume Buckets", summary.get("volume_buckets") or {}))
    lines.append("")
    lines.extend(_render_counter("## Symbol Types", summary.get("symbol_types") or {}))
    lines.append("")
    lines.extend(_render_counter("## Catalyst vs No Catalyst", summary.get("catalyst_vs_no_catalyst") or {}))
    lines.extend(["", "## Top Liquid Rejected Candidates"])
    lines.extend(_render_rows(report.get("top_liquid_rejected_candidates") or []))
    lines.extend(["", "## Top Repeated Junk Symbols"])
    lines.extend(_render_rows(report.get("top_repeated_junk_symbols") or []))
    lines.extend(["", "## Future Safe Exception Candidates"])
    lines.extend(_render_rows(report.get("future_safe_exception_candidates") or [], include_candidate_flag=True))
    return "\n".join(lines).rstrip() + "\n"


def write_dynamic_spread_tier_research_report(
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
    """Build and write dynamic spread-tier research artifacts."""
    data = Path(data_dir)
    day_s = _day_text(day)
    report = build_dynamic_spread_tier_research_report(
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
    json_path = out_dir / "dynamic_spread_tier_research.json"
    text_path = out_dir / "dynamic_spread_tier_research.md"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    text_path.write_text(render_dynamic_spread_tier_research_report(report), encoding="utf-8")
    return json_path, text_path, report
