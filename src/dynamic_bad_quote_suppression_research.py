"""Read-only dynamic bad-quote suppression research report."""

from __future__ import annotations

import gzip
import json
import math
import re
import subprocess
from collections import Counter, defaultdict
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
_SCAN_LINE_RE = re.compile(r"\bDYNAMIC_SCAN\s+(?P<symbol>[A-Z][A-Z0-9.\-]{0,12}):\s*(?P<body>.+)$")
_UNSTABLE_RE = re.compile(r"\bUnstable quote\s+(?P<symbol>[A-Z][A-Z0-9.\-]{0,12})\b")
_SYMBOL_RE = re.compile(r"\bsymbol=([A-Z][A-Z0-9.\-]{0,12})\b")
_STRONG_NEWS_SCORE = 7.0
_STRONG_CATALYST_SCORE = 0.7
_SUPPRESS_AFTER_COUNT = 3


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
    return day_s in line or compact in line or not re.search(r"\d{4}-\d{2}-\d{2}", line)


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


def _timestamp_from_line(line: str) -> str:
    iso = re.search(r"\b\d{4}-\d{2}-\d{2}[T ][0-9:.:\-+Z]+\b", line)
    if iso:
        return iso.group(0)
    syslog = re.match(r"([A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})", line.strip())
    if syslog:
        return syslog.group(1)
    return line.strip()[:24]


def _normalize_reason(value: Any) -> str:
    text = str(value or "").strip().lower().replace(" ", "_")
    if "bad_quote" in text or "bad quote" in text:
        return "bad_quote"
    if "unstable_quote" in text or "unstable quote" in text:
        return "unstable_quote"
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


def _price_fields(row: Mapping[str, Any]) -> dict[str, float | None]:
    price = _safe_float(row.get("price") or row.get("current_price") or row.get("paper_current_price") or row.get("last_price"))
    bid = _safe_float(row.get("bid") or row.get("bid_price"))
    ask = _safe_float(row.get("ask") or row.get("ask_price"))
    spread = _safe_float(row.get("spread_pct") or row.get("spread") or row.get("bid_ask_spread_pct"))
    if spread is None and bid is not None and ask is not None and bid > 0 and ask > 0:
        mid = (bid + ask) / 2.0
        if mid > 0:
            spread = abs(ask - bid) / mid * 100.0
    return {"price": price, "bid": bid, "ask": ask, "spread_pct": spread}


def _context_fields(row: Mapping[str, Any]) -> dict[str, float | None]:
    return {
        "volume": _safe_float(row.get("volume") or row.get("vol")),
        "relative_volume": _safe_float(row.get("relative_volume") or row.get("rel_volume") or row.get("rvol") or row.get("rel")),
        "gain_pct": _safe_float(row.get("gain_pct") or row.get("day_gain_pct") or row.get("gain")),
        "news_score": _safe_float(row.get("news_score")),
        "catalyst_score": _safe_float(row.get("catalyst_score")),
        "event_score": _safe_float(row.get("event_score")),
    }


def _is_strong_catalyst(row: Mapping[str, Any]) -> bool:
    return bool(
        (_safe_float(row.get("news_score")) or 0.0) >= _STRONG_NEWS_SCORE
        or (_safe_float(row.get("catalyst_score")) or 0.0) >= _STRONG_CATALYST_SCORE
        or (_safe_float(row.get("event_score")) or 0.0) >= _STRONG_NEWS_SCORE
    )


def _has_any_catalyst(row: Mapping[str, Any]) -> bool:
    return bool(
        (_safe_float(row.get("news_score")) or 0.0) > 0.0
        or (_safe_float(row.get("catalyst_score")) or 0.0) > 0.0
        or (_safe_float(row.get("event_score")) or 0.0) > 0.0
    )


def _event_from_history_row(raw: Mapping[str, Any], *, payload: Mapping[str, Any], path: Path, source_sequence: int) -> dict[str, Any] | None:
    reason = _normalize_reason(raw.get("rejection_reason") or raw.get("reason"))
    if reason not in {"bad_quote", "unstable_quote"}:
        return None
    symbol = str(raw.get("symbol") or "").strip().upper()
    if not symbol:
        return None
    return {
        "symbol": symbol,
        "timestamp": _candidate_timestamp(raw, payload),
        "reason": reason,
        **{key: _round(value) for key, value in _price_fields(raw).items()},
        **{key: _round(value, 4) for key, value in _context_fields(raw).items()},
        "accepted_later_marker": False,
        "tradable_later_marker": False,
        "strong_catalyst": _is_strong_catalyst(raw),
        "has_catalyst": _has_any_catalyst(raw),
        "source": "dynamic_scan_history",
        "evidence": f"{path.name}:{source_sequence}",
    }


def _load_history_events(
    *,
    data_dir: Path,
    day: str,
    user_id: str,
    history_dir: Path | None,
) -> tuple[list[dict[str, Any]], set[str], str]:
    paths, source_mode = _history_files(data_dir, day=day, user_id=user_id, history_dir=history_dir)
    events: list[dict[str, Any]] = []
    accepted_symbols: set[str] = set()
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
            if not isinstance(raw, Mapping):
                continue
            if _row_day(raw, payload, path) != day:
                continue
            symbol = str(raw.get("symbol") or "").strip().upper()
            if symbol and (raw.get("accepted") is True or raw in (payload.get("accepted") or []) or raw in (payload.get("selected") or [])):
                accepted_symbols.add(symbol)
            sequence += 1
            event = _event_from_history_row(raw, payload=payload, path=path, source_sequence=sequence)
            if event is not None:
                events.append(event)
    return events, accepted_symbols, source_mode


def _event_from_reject_line(line: str) -> dict[str, Any] | None:
    match = _SCAN_REJECT_RE.search(line)
    if not match:
        return None
    body = match.group("body")
    reason = _normalize_reason(body.split(" ", 1)[0] if body else "")
    if "bad quote" in body.lower():
        reason = "bad_quote"
    elif "unstable quote" in body.lower():
        reason = "unstable_quote"
    if reason not in {"bad_quote", "unstable_quote"}:
        return None
    symbol = match.group("symbol").upper()
    kv = _parse_kv(body)
    return {
        "symbol": symbol,
        "timestamp": _timestamp_from_line(line),
        "reason": reason,
        **{key: _round(value) for key, value in _price_fields(kv).items()},
        **{key: _round(value, 4) for key, value in _context_fields(kv).items()},
        "accepted_later_marker": False,
        "tradable_later_marker": False,
        "strong_catalyst": _is_strong_catalyst(kv),
        "has_catalyst": _has_any_catalyst(kv),
        "source": "logs",
        "evidence": line.strip(),
    }


def _event_from_unstable_line(line: str) -> dict[str, Any] | None:
    match = _UNSTABLE_RE.search(line)
    if not match:
        return None
    symbol = match.group("symbol").upper()
    kv = _parse_kv(line)
    return {
        "symbol": symbol,
        "timestamp": _timestamp_from_line(line),
        "reason": "unstable_quote",
        **{key: _round(value) for key, value in _price_fields(kv).items()},
        **{key: _round(value, 4) for key, value in _context_fields(kv).items()},
        "accepted_later_marker": False,
        "tradable_later_marker": False,
        "strong_catalyst": _is_strong_catalyst(kv),
        "has_catalyst": _has_any_catalyst(kv),
        "source": "logs",
        "evidence": line.strip(),
    }


def _scan_context_by_symbol(lines: Sequence[str]) -> dict[str, dict[str, Any]]:
    contexts: dict[str, dict[str, Any]] = {}
    for line in lines:
        match = _SCAN_LINE_RE.search(line)
        if not match or "reject" in line:
            continue
        symbol = match.group("symbol").upper()
        kv = _parse_kv(match.group("body"))
        contexts[symbol] = {**_price_fields(kv), **_context_fields(kv)}
    return contexts


def _accepted_or_tradable_from_lines(lines: Sequence[str]) -> tuple[set[str], set[str]]:
    accepted: set[str] = set()
    tradable: set[str] = set()
    for line in lines:
        symbol = ""
        selected = "DYNAMIC_SELECTED" in line or "DYNAMIC_SCAN selected" in line or "DYNAMIC_ACCEPTED" in line
        downstream = "ENTRY_EVAL_PASS" in line or "ORDER_SUBMITTED" in line or "ALLOCATOR_DISPATCH_END result=submitted" in line
        if selected or downstream:
            sym_match = _SYMBOL_RE.search(line)
            if sym_match:
                symbol = sym_match.group(1).upper()
        if symbol:
            if selected:
                accepted.add(symbol)
            if selected or downstream:
                tradable.add(symbol)
    return accepted, tradable


def _core_symbols(config: Mapping[str, Any] | None) -> set[str]:
    symbols = set(ETF_SYMBOLS)
    cfg = config if isinstance(config, Mapping) else {}
    for section_name in ("universe", "execution"):
        section = cfg.get(section_name) if isinstance(cfg.get(section_name), Mapping) else {}
        for key in ("symbols", "core_symbols", "large_cap_symbols"):
            rows = section.get(key) if isinstance(section, Mapping) else None
            if isinstance(rows, list):
                symbols.update(str(row or "").strip().upper() for row in rows if str(row or "").strip())
    return symbols


def _load_config(project_root: Path, config: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if isinstance(config, Mapping):
        return config
    path = project_root / "config" / "default.yaml"
    if not path.exists():
        return {}
    try:
        return load_config(path)
    except Exception:
        return {}


def _mean(rows: Sequence[Mapping[str, Any]], key: str) -> float | None:
    values = [float(value) for row in rows if (value := _safe_float(row.get(key))) is not None]
    return round(sum(values) / len(values), 4) if values else None


def _recommendation(symbol: str, events: Sequence[Mapping[str, Any]], *, core_symbols: set[str]) -> tuple[str, str]:
    if symbol in core_symbols:
        return "do_not_suppress", "core_or_etf_symbol"
    if any(bool(event.get("strong_catalyst")) for event in events):
        return "do_not_suppress", "strong_catalyst_present"
    if any(bool(event.get("has_catalyst")) for event in events):
        return "suppress_only_if_repeats_without_catalyst", "catalyst_present"
    if len(events) >= _SUPPRESS_AFTER_COUNT:
        return "suppress_rest_of_day_after_3_bad_quotes", "repeated_no_catalyst_bad_quotes"
    return "watch", "below_repeat_threshold"


def _aggregate_symbol(symbol: str, events: Sequence[Mapping[str, Any]], *, core_symbols: set[str]) -> dict[str, Any]:
    reason_counts = Counter(str(event.get("reason") or "unknown") for event in events)
    zero_bid_ask_count = sum(
        1
        for event in events
        if (_safe_float(event.get("bid")) or 0.0) <= 0.0 or (_safe_float(event.get("ask")) or 0.0) <= 0.0
    )
    ask_zero_count = sum(1 for event in events if (_safe_float(event.get("ask")) or 0.0) <= 0.0)
    price_zero_count = sum(1 for event in events if (_safe_float(event.get("price")) or 0.0) <= 0.0)
    action, reason = _recommendation(symbol, events, core_symbols=core_symbols)
    return {
        "symbol": symbol,
        "count": len(events),
        "bad_quote_count": reason_counts.get("bad_quote", 0),
        "unstable_quote_count": reason_counts.get("unstable_quote", 0),
        "zero_bid_or_ask_count": zero_bid_ask_count,
        "ask_zero_count": ask_zero_count,
        "price_zero_count": price_zero_count,
        "average_volume": _mean(events, "volume"),
        "average_relative_volume": _mean(events, "relative_volume"),
        "average_gain_pct": _mean(events, "gain_pct"),
        "average_news_score": _mean(events, "news_score"),
        "average_catalyst_score": _mean(events, "catalyst_score"),
        "average_event_score": _mean(events, "event_score"),
        "latest_price": events[-1].get("price"),
        "latest_bid": events[-1].get("bid"),
        "latest_ask": events[-1].get("ask"),
        "latest_spread_pct": events[-1].get("spread_pct"),
        "ever_accepted_later": any(bool(event.get("accepted_later_marker")) for event in events),
        "became_tradable_later": any(bool(event.get("tradable_later_marker")) for event in events),
        "strong_catalyst_seen": any(bool(event.get("strong_catalyst")) for event in events),
        "has_catalyst_seen": any(bool(event.get("has_catalyst")) for event in events),
        "is_core_or_etf": symbol in core_symbols,
        "recommended_suppression": action,
        "recommendation_reason": reason,
    }


def build_dynamic_bad_quote_suppression_research(
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
    """Build a read-only bad-quote suppression research report."""
    day_s = _day_text(day)
    root = Path(project_root)
    data = Path(data_dir)
    history_path = Path(history_dir) if history_dir is not None else None
    cfg = _load_config(root, config)
    core = _core_symbols(cfg)
    lines = _load_log_lines(project_root=root, data_dir=data, day=day_s, log_text=log_text, log_files=log_files)
    events, accepted_from_history, source_mode = _load_history_events(data_dir=data, day=day_s, user_id=user_id, history_dir=history_path)
    scan_context = _scan_context_by_symbol(lines)
    accepted_from_lines, tradable_from_lines = _accepted_or_tradable_from_lines(lines)
    accepted = set(accepted_from_history) | accepted_from_lines
    tradable = set(accepted) | tradable_from_lines
    for line in lines:
        event = _event_from_reject_line(line) or _event_from_unstable_line(line)
        if event is None:
            continue
        context = scan_context.get(str(event.get("symbol") or ""))
        if context:
            for key, value in context.items():
                if event.get(key) is None:
                    event[key] = _round(value, 4)
            event["strong_catalyst"] = bool(event.get("strong_catalyst") or _is_strong_catalyst(event))
            event["has_catalyst"] = bool(event.get("has_catalyst") or _has_any_catalyst(event))
        events.append(event)

    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for event in events:
        key = (
            str(event.get("timestamp") or ""),
            str(event.get("symbol") or ""),
            str(event.get("reason") or ""),
            str(event.get("evidence") or "")[:160],
        )
        if key in seen:
            continue
        seen.add(key)
        symbol = str(event.get("symbol") or "")
        event["accepted_later_marker"] = symbol in accepted
        event["tradable_later_marker"] = symbol in tradable
        deduped.append(event)

    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for event in deduped:
        symbol = str(event.get("symbol") or "")
        if symbol:
            grouped[symbol].append(event)
    symbol_rows = [
        _aggregate_symbol(symbol, sorted(rows, key=lambda row: str(row.get("timestamp") or "")), core_symbols=core)
        for symbol, rows in grouped.items()
    ]
    symbol_rows.sort(key=lambda row: (-int(row["count"]), str(row["symbol"])))
    repeated = [row for row in symbol_rows if int(row["count"]) >= 2]
    recommended = [row for row in symbol_rows if str(row.get("recommended_suppression")) == "suppress_rest_of_day_after_3_bad_quotes"]
    return {
        "report": "dynamic_bad_quote_suppression_research",
        "research_only": True,
        "date": day_s,
        "user_id": str(user_id or "default"),
        "source": {
            "history_mode": source_mode,
            "log_line_count": len(lines),
            "event_count": len(deduped),
        },
        "summary": {
            "symbols": len(symbol_rows),
            "events": len(deduped),
            "repeated_symbols": len(repeated),
            "recommended_suppression_symbols": len(recommended),
            "zero_bid_or_ask_events": sum(1 for event in deduped if (_safe_float(event.get("bid")) or 0.0) <= 0.0 or (_safe_float(event.get("ask")) or 0.0) <= 0.0),
            "ask_zero_events": sum(1 for event in deduped if (_safe_float(event.get("ask")) or 0.0) <= 0.0),
            "price_zero_events": sum(1 for event in deduped if (_safe_float(event.get("price")) or 0.0) <= 0.0),
        },
        "recommended_rules": [
            "suppress rest of day after N repeated bad_quote/unstable_quote events for scanner-only symbols",
            "suppress only if no catalyst is present",
            "never suppress core/ETF list symbols",
            "never suppress if strong catalyst is present",
        ],
        "symbols": symbol_rows,
        "repeated_symbols": repeated,
        "events": sorted(deduped, key=lambda row: (str(row.get("timestamp") or ""), str(row.get("symbol") or ""))),
    }


def render_dynamic_bad_quote_suppression_research(report: Mapping[str, Any]) -> str:
    """Render bad-quote suppression research as Markdown."""
    summary = report.get("summary") if isinstance(report.get("summary"), Mapping) else {}
    lines = [
        f"# Dynamic Bad Quote Suppression Research {report.get('date')} user={report.get('user_id')}",
        "",
        "Read-only research: no trading behavior, thresholds, orders, exits, stops, or sizing changed.",
        "",
        "## Summary",
        f"- bad quote events: {summary.get('events', 0)}",
        f"- symbols: {summary.get('symbols', 0)}",
        f"- repeated symbols: {summary.get('repeated_symbols', 0)}",
        f"- recommended suppression symbols: {summary.get('recommended_suppression_symbols', 0)}",
        f"- zero bid/ask events: {summary.get('zero_bid_or_ask_events', 0)}",
        f"- ask=0 events: {summary.get('ask_zero_events', 0)}",
        f"- price=0 events: {summary.get('price_zero_events', 0)}",
        "",
        "## Recommended Rules",
    ]
    for rule in report.get("recommended_rules") or []:
        lines.append(f"- {rule}")
    lines.extend(
        [
            "",
            "## Repeated Bad Quote Symbols",
            "| symbol | count | bad_quote | unstable_quote | zero bid/ask | ask=0 | price=0 | avg vol | avg RVOL | avg gain% | catalyst avg | accepted later | tradable later | recommendation | reason |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- | --- | --- |",
        ]
    )
    repeated = report.get("repeated_symbols") if isinstance(report.get("repeated_symbols"), list) else []
    if not repeated:
        lines.append("| none | | | | | | | | | | | | | | |")
    for row in repeated:
        lines.append(
            f"| {row.get('symbol')} | {row.get('count')} | {row.get('bad_quote_count')} | {row.get('unstable_quote_count')} | "
            f"{row.get('zero_bid_or_ask_count')} | {row.get('ask_zero_count')} | {row.get('price_zero_count')} | "
            f"{row.get('average_volume')} | {row.get('average_relative_volume')} | {row.get('average_gain_pct')} | "
            f"{row.get('average_catalyst_score')} | {row.get('ever_accepted_later')} | {row.get('became_tradable_later')} | "
            f"{row.get('recommended_suppression')} | {row.get('recommendation_reason')} |"
        )
    lines.extend(
        [
            "",
            "## Events",
            "| timestamp | symbol | reason | price | bid | ask | spread | volume | RVOL | gain% | news | catalyst | event | source |",
            "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
        ]
    )
    for event in report.get("events") or []:
        lines.append(
            f"| {event.get('timestamp')} | {event.get('symbol')} | {event.get('reason')} | {event.get('price')} | "
            f"{event.get('bid')} | {event.get('ask')} | {event.get('spread_pct')} | {event.get('volume')} | "
            f"{event.get('relative_volume')} | {event.get('gain_pct')} | {event.get('news_score')} | "
            f"{event.get('catalyst_score')} | {event.get('event_score')} | {event.get('source')} |"
        )
    return "\n".join(lines).rstrip() + "\n"


def write_dynamic_bad_quote_suppression_research(
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
    """Build and write dynamic bad-quote suppression research artifacts."""
    data = Path(data_dir)
    day_s = _day_text(day)
    report = build_dynamic_bad_quote_suppression_research(
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
    json_path = out_dir / "dynamic_bad_quote_suppression_research.json"
    text_path = out_dir / "dynamic_bad_quote_suppression_research.md"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    text_path.write_text(render_dynamic_bad_quote_suppression_research(report), encoding="utf-8")
    return json_path, text_path, report


__all__ = [
    "build_dynamic_bad_quote_suppression_research",
    "render_dynamic_bad_quote_suppression_research",
    "write_dynamic_bad_quote_suppression_research",
]
