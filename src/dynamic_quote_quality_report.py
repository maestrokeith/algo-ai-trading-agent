"""Read-only dynamic unstable quote quality report."""

from __future__ import annotations

import gzip
import json
import math
import re
import subprocess
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from src.dynamic_rvol_sensitivity import _history_paths_for_date

_KV_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=([^ ]+)")
_ISO_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
_COMPACT_DATE_RE = re.compile(r"(\d{8})")
_SYMBOL_RE = re.compile(r"\bsymbol=([A-Z][A-Z0-9.\-]{0,9})\b")
_SCAN_REJECT_RE = re.compile(r"\bDYNAMIC_SCAN reject\s+(?P<symbol>[A-Z][A-Z0-9.\-]{0,9}):\s*(?P<body>.+)$")
_DYNAMIC_SELECT_RE = re.compile(r"\bDYNAMIC_SELECTED\s+symbol=([A-Z][A-Z0-9.\-]{0,9})\b")
_ENTRY_PASS_RE = re.compile(r"\bENTRY_EVAL_PASS\s+symbol=([A-Z][A-Z0-9.\-]{0,9})\b")
_STALE_QUOTE_SECONDS = 60.0
_UNSTABLE_SPREAD_PCT = 15.0


@dataclass(frozen=True)
class RetryState:
    attempted: bool = False
    succeeded: bool = False
    failed: bool = False
    attempts: int = 0


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


def _timestamp_from_line(line: str) -> str:
    text = line.strip()
    iso = re.search(r"\b\d{4}-\d{2}-\d{2}[T ][0-9:.:\-+Z]+\b", text)
    if iso:
        return iso.group(0)
    syslog = re.match(r"([A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})", text)
    if syslog:
        return syslog.group(1)
    return text[:24]


def _symbol_from_line(line: str) -> str:
    match = _SYMBOL_RE.search(line)
    if match:
        return match.group(1).upper()
    scan = _SCAN_REJECT_RE.search(line)
    if scan:
        return scan.group("symbol").upper()
    for regex in (_DYNAMIC_SELECT_RE, _ENTRY_PASS_RE):
        match = regex.search(line)
        if match:
            return match.group(1).upper()
    return ""


def _normalize_reason(value: Any) -> str:
    text = str(value or "").strip().lower().replace(" ", "_")
    return re.sub(r"[^a-z0-9_]+", "_", text).strip("_")


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


def _root_causes_from_values(
    *,
    bid: float | None,
    ask: float | None,
    spread_pct: float | None,
    quote_age_seconds: float | None,
    volume: float | None,
    text: str = "",
) -> list[str]:
    lower = text.lower()
    causes: list[str] = []
    if bid is None or bid <= 0:
        causes.append("missing_bid")
    if ask is None or ask <= 0:
        causes.append("missing_ask")
    if bid is not None and ask is not None and bid > 0 and ask > 0 and ask < bid:
        causes.append("crossed_market")
    if spread_pct is not None and spread_pct > _UNSTABLE_SPREAD_PCT:
        causes.append("spread_too_wide")
    if quote_age_seconds is not None and quote_age_seconds > _STALE_QUOTE_SECONDS:
        causes.append("stale_quote")
    if volume is not None and volume <= 0:
        causes.append("zero_volume")
    if any(token in lower for token in ("halted", "halt", "suspended", "security_state=halt", "trading_status=halt")):
        causes.append("halted_security_state")
    return causes or ["unknown_unstable_quote"]


def _primary_cause(causes: Sequence[str]) -> str:
    priority = (
        "halted_security_state",
        "missing_bid",
        "missing_ask",
        "crossed_market",
        "stale_quote",
        "zero_volume",
        "spread_too_wide",
        "unknown_unstable_quote",
    )
    for cause in priority:
        if cause in causes:
            return cause
    return str(causes[0]) if causes else "unknown_unstable_quote"


def _event_from_row(raw: Mapping[str, Any], *, payload: Mapping[str, Any], path: Path, source_sequence: int) -> dict[str, Any] | None:
    reason = _normalize_reason(raw.get("rejection_reason") or raw.get("reason"))
    if reason != "unstable_quote":
        return None
    symbol = str(raw.get("symbol") or "").strip().upper()
    if not symbol:
        return None
    bid = _safe_float(raw.get("bid") or raw.get("bid_price"))
    ask = _safe_float(raw.get("ask") or raw.get("ask_price"))
    spread_pct = _safe_float(raw.get("spread_pct") or raw.get("bid_ask_spread_pct") or raw.get("spread"))
    if spread_pct is None and bid is not None and ask is not None and bid > 0 and ask > 0:
        mid = (bid + ask) / 2.0
        if mid > 0:
            spread_pct = abs(ask - bid) / mid * 100.0
    quote_age_seconds = _safe_float(raw.get("quote_age_seconds") or raw.get("quote_age_sec") or raw.get("age_seconds"))
    volume = _safe_float(raw.get("volume"))
    text = json.dumps(raw, sort_keys=True, default=str)
    causes = _root_causes_from_values(
        bid=bid,
        ask=ask,
        spread_pct=spread_pct,
        quote_age_seconds=quote_age_seconds,
        volume=volume,
        text=text,
    )
    return {
        "symbol": symbol,
        "timestamp": _candidate_timestamp(raw, payload),
        "root_cause": _primary_cause(causes),
        "root_causes": causes,
        "bid": _round(bid),
        "ask": _round(ask),
        "spread_pct": _round(spread_pct),
        "quote_age_seconds": _round(quote_age_seconds),
        "volume": _round(volume, 2),
        "retry_attempted": False,
        "retry_succeeded": False,
        "retry_failed": False,
        "became_tradable_later": False,
        "source": "dynamic_scan_history",
        "source_file": str(path),
        "source_sequence": source_sequence,
        "evidence": f"{path.name}:{source_sequence}",
    }


def _load_history_events(
    *,
    data_dir: Path,
    day: str,
    user_id: str,
    history_dir: Path | None,
) -> tuple[list[dict[str, Any]], str]:
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
            if not isinstance(raw, Mapping):
                continue
            if _row_day(raw, payload, path) != day:
                continue
            sequence += 1
            event = _event_from_row(raw, payload=payload, path=path, source_sequence=sequence)
            if event is not None:
                events.append(event)
    return events, source_mode


def _event_from_unstable_line(line: str) -> dict[str, Any] | None:
    lower = line.lower()
    if "unstable_quote" not in lower and "unstable quote" not in lower:
        return None
    if (
        "quote_retry_start" in lower
        or "quote_retry_success" in lower
        or "quote_retry_failed" in lower
        or "quote_retry_final_reject" in lower
    ):
        return None
    symbol = _symbol_from_line(line)
    if not symbol:
        return None
    kv = _parse_kv(line)
    bid = _safe_float(kv.get("bid") or kv.get("bid_price"))
    ask = _safe_float(kv.get("ask") or kv.get("ask_price"))
    spread_pct = _safe_float(kv.get("spread_pct") or kv.get("spread"))
    quote_age_seconds = _safe_float(kv.get("quote_age_seconds") or kv.get("quote_age_sec") or kv.get("age_seconds"))
    volume = _safe_float(kv.get("volume"))
    if spread_pct is None and bid is not None and ask is not None and bid > 0 and ask > 0:
        mid = (bid + ask) / 2.0
        if mid > 0:
            spread_pct = abs(ask - bid) / mid * 100.0
    causes = _root_causes_from_values(
        bid=bid,
        ask=ask,
        spread_pct=spread_pct,
        quote_age_seconds=quote_age_seconds,
        volume=volume,
        text=line,
    )
    return {
        "symbol": symbol,
        "timestamp": _timestamp_from_line(line),
        "root_cause": _primary_cause(causes),
        "root_causes": causes,
        "bid": _round(bid),
        "ask": _round(ask),
        "spread_pct": _round(spread_pct),
        "quote_age_seconds": _round(quote_age_seconds),
        "volume": _round(volume, 2),
        "retry_attempted": False,
        "retry_succeeded": False,
        "retry_failed": False,
        "became_tradable_later": False,
        "source": "logs",
        "source_file": None,
        "source_sequence": None,
        "evidence": line.strip(),
    }


def _retry_state_by_symbol(lines: Sequence[str]) -> dict[str, RetryState]:
    mutable: dict[str, dict[str, Any]] = defaultdict(lambda: {"attempted": False, "succeeded": False, "failed": False, "attempts": 0})
    for line in lines:
        symbol = _symbol_from_line(line)
        if not symbol:
            continue
        kv = _parse_kv(line)
        state = mutable[symbol]
        if "QUOTE_RETRY_START" in line and str(kv.get("reason") or "") == "unstable_quote":
            state["attempted"] = True
            state["attempts"] = max(int(state["attempts"] or 0), int(_safe_float(kv.get("attempt")) or 0))
        elif "QUOTE_RETRY_SUCCESS" in line:
            state["succeeded"] = True
            state["attempts"] = max(int(state["attempts"] or 0), int(_safe_float(kv.get("attempt")) or 0))
        elif "QUOTE_RETRY_FAILED" in line or "QUOTE_RETRY_FINAL_REJECT" in line:
            state["failed"] = True
            state["attempted"] = True
            state["attempts"] = max(int(state["attempts"] or 0), int(_safe_float(kv.get("attempts")) or 0))
    return {symbol: RetryState(**state) for symbol, state in mutable.items()}


def _tradable_symbols_from_lines(lines: Sequence[str]) -> set[str]:
    tradable: set[str] = set()
    for line in lines:
        if "DYNAMIC_SELECTED" in line or "ENTRY_EVAL_PASS" in line or "ORDER_SUBMITTED" in line or "ALLOCATOR_DISPATCH_END result=submitted" in line:
            symbol = _symbol_from_line(line)
            if symbol:
                tradable.add(symbol)
    return tradable


def _accepted_symbols_from_history(data_dir: Path, *, day: str, user_id: str, history_dir: Path | None) -> set[str]:
    paths, _ = _history_files(data_dir, day=day, user_id=user_id, history_dir=history_dir)
    symbols: set[str] = set()
    for path in paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(payload, Mapping):
            continue
        rows = payload.get("accepted") or payload.get("selected") or []
        if not isinstance(rows, list):
            continue
        for raw in rows:
            if isinstance(raw, Mapping):
                symbol = str(raw.get("symbol") or "").strip().upper()
            else:
                symbol = str(raw or "").strip().upper()
            if symbol:
                symbols.add(symbol)
    return symbols


def _event_key(event: Mapping[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(event.get("timestamp") or ""),
        str(event.get("symbol") or ""),
        str(event.get("root_cause") or ""),
        str(event.get("evidence") or "")[:120],
    )


def build_dynamic_quote_quality_report(
    *,
    project_root: Path | str = ".",
    data_dir: Path | str = "data",
    day: date | str,
    user_id: str,
    history_dir: Path | str | None = None,
    log_text: str | None = None,
    log_files: Sequence[Path | str] | None = None,
) -> dict[str, Any]:
    """Build a dynamic unstable quote root-cause report without trading side effects."""
    day_s = _day_text(day)
    root = Path(project_root)
    data = Path(data_dir)
    history_path = Path(history_dir) if history_dir is not None else None
    lines = _load_log_lines(project_root=root, data_dir=data, day=day_s, log_text=log_text, log_files=log_files)
    events, source_mode = _load_history_events(data_dir=data, day=day_s, user_id=user_id, history_dir=history_path)
    events.extend(event for event in (_event_from_unstable_line(line) for line in lines) if event is not None)

    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for event in events:
        key = _event_key(event)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(event)

    retries = _retry_state_by_symbol(lines)
    tradable = _tradable_symbols_from_lines(lines)
    tradable.update(_accepted_symbols_from_history(data, day=day_s, user_id=user_id, history_dir=history_path))

    for event in deduped:
        symbol = str(event.get("symbol") or "")
        retry = retries.get(symbol, RetryState())
        event["retry_attempted"] = retry.attempted
        event["retry_succeeded"] = retry.succeeded
        event["retry_failed"] = retry.failed
        event["retry_attempts"] = retry.attempts
        event["became_tradable_later"] = symbol in tradable

    root_counts: Counter[str] = Counter()
    symbol_counts: Counter[str] = Counter()
    retry_attempted_symbols: set[str] = set()
    retry_succeeded_symbols: set[str] = set()
    retry_failed_symbols: set[str] = set()
    for event in deduped:
        root_counts[str(event.get("root_cause") or "unknown_unstable_quote")] += 1
        symbol = str(event.get("symbol") or "")
        if symbol:
            symbol_counts[symbol] += 1
            if event.get("retry_attempted"):
                retry_attempted_symbols.add(symbol)
            if event.get("retry_succeeded"):
                retry_succeeded_symbols.add(symbol)
            if event.get("retry_failed"):
                retry_failed_symbols.add(symbol)

    spreads = [value for value in (_safe_float(event.get("spread_pct")) for event in deduped) if value is not None]
    quote_ages = [value for value in (_safe_float(event.get("quote_age_seconds")) for event in deduped) if value is not None]
    recovery_rate = (
        round(len(retry_succeeded_symbols) / len(retry_attempted_symbols), 4) if retry_attempted_symbols else None
    )

    return {
        "report": "dynamic_quote_quality",
        "date": day_s,
        "user_id": str(user_id or "default"),
        "source": {
            "history_mode": source_mode,
            "log_line_count": len(lines),
            "event_count": len(deduped),
        },
        "summary": {
            "total_unstable_quotes": len(deduped),
            "root_cause_counts": dict(root_counts.most_common()),
            "symbols_most_affected": [
                {"symbol": symbol, "count": count} for symbol, count in symbol_counts.most_common(10)
            ],
            "average_spread": round(sum(spreads) / len(spreads), 4) if spreads else None,
            "average_quote_age": round(sum(quote_ages) / len(quote_ages), 4) if quote_ages else None,
            "retry_attempted": len(retry_attempted_symbols),
            "retry_succeeded": len(retry_succeeded_symbols),
            "retry_failed": len(retry_failed_symbols),
            "retry_recovery_rate": recovery_rate,
            "symbols_that_became_tradable_later": sorted(
                {
                    str(event.get("symbol") or "")
                    for event in deduped
                    if event.get("became_tradable_later") and event.get("symbol")
                }
            ),
        },
        "events": sorted(
            deduped,
            key=lambda row: (str(row.get("timestamp") or ""), str(row.get("symbol") or ""), str(row.get("source") or "")),
        ),
    }


def render_dynamic_quote_quality_report(report: Mapping[str, Any]) -> str:
    """Render a dynamic quote quality report as Markdown."""
    summary = report.get("summary") if isinstance(report.get("summary"), Mapping) else {}
    lines = [
        f"# Dynamic Quote Quality Report {report.get('date')} user={report.get('user_id')}",
        "",
        "## Summary",
        f"- total unstable_quote rejections: {summary.get('total_unstable_quotes', 0)}",
        f"- average spread: {summary.get('average_spread')}",
        f"- average quote age: {summary.get('average_quote_age')}",
        f"- retry attempted symbols: {summary.get('retry_attempted', 0)}",
        f"- retry succeeded symbols: {summary.get('retry_succeeded', 0)}",
        f"- retry failed symbols: {summary.get('retry_failed', 0)}",
        f"- retry recovery rate: {summary.get('retry_recovery_rate')}",
        f"- symbols that became tradable later: {', '.join(summary.get('symbols_that_became_tradable_later') or []) or 'none'}",
        "",
        "## Root Cause Counts",
    ]
    for cause, count in dict(summary.get("root_cause_counts") or {}).items():
        lines.append(f"- {cause}: {count}")
    lines.extend(["", "## Symbols Most Affected"])
    for row in summary.get("symbols_most_affected") or []:
        lines.append(f"- {row.get('symbol')}: {row.get('count')}")
    lines.extend(
        [
            "",
            "## Events",
            "| symbol | timestamp | root_cause | root_causes | spread_pct | quote_age_seconds | bid | ask | volume | retry_attempted | retry_succeeded | retry_failed | became_tradable_later |",
            "| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | --- | --- | --- | --- |",
        ]
    )
    for event in report.get("events") or []:
        lines.append(
            "| {symbol} | {timestamp} | {root_cause} | {root_causes} | {spread_pct} | {quote_age_seconds} | {bid} | {ask} | {volume} | {retry_attempted} | {retry_succeeded} | {retry_failed} | {became_tradable_later} |".format(
                symbol=event.get("symbol") or "",
                timestamp=event.get("timestamp") or "",
                root_cause=event.get("root_cause") or "",
                root_causes=", ".join(event.get("root_causes") or []),
                spread_pct=event.get("spread_pct"),
                quote_age_seconds=event.get("quote_age_seconds"),
                bid=event.get("bid"),
                ask=event.get("ask"),
                volume=event.get("volume"),
                retry_attempted=bool(event.get("retry_attempted")),
                retry_succeeded=bool(event.get("retry_succeeded")),
                retry_failed=bool(event.get("retry_failed")),
                became_tradable_later=bool(event.get("became_tradable_later")),
            )
        )
    return "\n".join(lines).rstrip() + "\n"


def write_dynamic_quote_quality_report(
    *,
    project_root: Path | str = ".",
    data_dir: Path | str = "data",
    day: date | str,
    user_id: str,
    history_dir: Path | str | None = None,
    log_text: str | None = None,
    log_files: Sequence[Path | str] | None = None,
) -> tuple[Path, Path, dict[str, Any]]:
    """Build and write dynamic quote quality JSON and Markdown artifacts."""
    data = Path(data_dir)
    day_s = _day_text(day)
    report = build_dynamic_quote_quality_report(
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
    json_path = out_dir / "dynamic_quote_quality.json"
    text_path = out_dir / "dynamic_quote_quality.md"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    text_path.write_text(render_dynamic_quote_quality_report(report), encoding="utf-8")
    return json_path, text_path, report
