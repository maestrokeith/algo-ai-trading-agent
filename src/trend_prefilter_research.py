"""Research-only analysis for repeated trend-prefilter skips."""

from __future__ import annotations

import gzip
import json
import math
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

_ET = ZoneInfo("America/New_York")
_UTC = ZoneInfo("UTC")
_ISO_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
_COMPACT_DATE_RE = re.compile(r"(\d{8})")
_TREND_SKIP_RE = re.compile(
    r"\bSKIP\s+(?P<symbol>[A-Z][A-Z0-9.\-]{0,9}):\s+reason=(?P<reason>.*below MAs \(trend prefilter\).*)$"
)
_ENTRY_EVAL_RE = re.compile(
    r"\b(?P<symbol>[A-Z][A-Z0-9.\-]{0,9})\s+ENTRY_EVAL\s+route=(?P<route>[^ ]+).*?"
    r"\bfinal=(?P<final>[TF]|true|false|True|False)\b.*?\breason=(?P<reason>.+?)(?=\s+[A-Za-z_][A-Za-z0-9_]*=|$)"
)


@dataclass(frozen=True)
class TrendPrefilterPaths:
    json_path: Path
    text_path: Path


def _safe_user(user_id: str) -> str:
    return "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in str(user_id or "default")) or "default"


def _safe_float(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return out if math.isfinite(out) else None


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


def _line_timestamp(line: str, *, day: str) -> datetime | None:
    match = re.match(r"^[A-Z][a-z]{2}\s+\d{1,2}\s+(\d{2}:\d{2}:\d{2})\b", line)
    if match:
        try:
            return datetime.fromisoformat(f"{day}T{match.group(1)}").replace(tzinfo=_ET)
        except ValueError:
            return None
    iso = re.search(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?)", line)
    return _parse_timestamp(iso.group(1)) if iso else None


def _read_text(path: Path) -> str:
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    return path.read_text(encoding="utf-8", errors="replace")


def discover_trend_prefilter_log_paths(
    *,
    project_root: Path | str = ".",
    day: str,
    extra_paths: Sequence[Path | str] | None = None,
) -> list[Path]:
    root = Path(project_root)
    paths: list[Path] = []
    for base in (
        root / "data" / "logs",
        root / "data" / "debug_logs",
        root / "reports" / "debug",
        root,
    ):
        if not base.exists():
            continue
        iterator = base.rglob("*") if base.name == "debug_logs" else base.glob("*")
        for path in iterator:
            if not path.is_file() or path.suffix not in {".log", ".txt", ".gz"}:
                continue
            path_day = _date_from_path(path)
            if path_day not in {None, day} and day not in path.name:
                continue
            paths.append(path)
    for raw in extra_paths or []:
        path = Path(raw)
        if path.exists() and path.is_file():
            paths.append(path)
    return sorted(dict.fromkeys(paths))


def _local_bar_roots(data_dir: Path, bars_dir: Path | str | None) -> list[Path]:
    if bars_dir is not None:
        return [Path(bars_dir)]
    return [
        data_dir / "historical_bars",
        data_dir / "bars",
        data_dir / "market_bars",
        data_dir / "intraday_bars",
    ]


def _load_local_bars_for_symbol(
    *,
    data_dir: Path,
    bars_dir: Path | str | None,
    symbol: str,
    day: str,
) -> pd.DataFrame | None:
    sym = str(symbol or "").strip().upper()
    if not sym:
        return None
    compact = day.replace("-", "")
    candidates: list[Path] = []
    for root in _local_bar_roots(data_dir, bars_dir):
        if not root.exists():
            continue
        for suffix in ("csv", "json"):
            candidates.extend(root.glob(f"**/{sym}*{day}*.{suffix}"))
            candidates.extend(root.glob(f"**/{day}*{sym}*.{suffix}"))
            candidates.extend(root.glob(f"**/{sym}*{compact}*.{suffix}"))
            candidates.extend(root.glob(f"**/{compact}*{sym}*.{suffix}"))
            candidates.extend(root.glob(f"**/{sym}.{suffix}"))
    for path in sorted(dict.fromkeys(candidates)):
        try:
            if path.suffix.lower() == ".csv":
                df = pd.read_csv(path)
            else:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                rows = loaded.get("bars") if isinstance(loaded, Mapping) else loaded
                df = pd.DataFrame(rows)
        except Exception:
            continue
        if isinstance(df, pd.DataFrame) and not df.empty:
            return df
    return None


def _bar_timestamps_utc(bars: pd.DataFrame) -> pd.Series | None:
    for col in ("timestamp", "time", "ts", "datetime", "date"):
        if col in bars.columns:
            parsed = pd.to_datetime(bars[col], utc=True, errors="coerce")
            return parsed if parsed.notna().any() else None
    if isinstance(bars.index, pd.DatetimeIndex):
        idx = bars.index
        if idx.tz is None:
            idx = idx.tz_localize("UTC", ambiguous="infer", nonexistent="shift_forward")
        return pd.Series(idx.tz_convert("UTC"), index=bars.index)
    return None


def _outcome_from_bars(bars: pd.DataFrame | None, *, first_rejection: datetime, day: str) -> dict[str, Any]:
    if bars is None or bars.empty:
        return {
            "first_rejection_price": None,
            "close_price": None,
            "max_high_after_first_rejection": None,
            "price_change_to_close_pct": None,
            "max_gain_after_first_rejection_pct": None,
            "would_have_been_profitable_to_close": None,
            "outcome_available": False,
            "missing_reason": "missing_local_bars",
        }
    ts = _bar_timestamps_utc(bars)
    if ts is None:
        return {
            "first_rejection_price": None,
            "close_price": None,
            "max_high_after_first_rejection": None,
            "price_change_to_close_pct": None,
            "max_gain_after_first_rejection_pct": None,
            "would_have_been_profitable_to_close": None,
            "outcome_available": False,
            "missing_reason": "missing_bar_timestamps",
        }
    frame = bars.copy()
    frame["_ts_utc"] = ts
    target_day = first_rejection.astimezone(_ET).date().isoformat()
    target_day = day if target_day != day else target_day
    local_day = frame["_ts_utc"].dt.tz_convert(_ET).dt.date.astype(str) == target_day
    later = frame.loc[local_day & (frame["_ts_utc"] >= first_rejection.astimezone(_UTC))]
    if later.empty:
        return {
            "first_rejection_price": None,
            "close_price": None,
            "max_high_after_first_rejection": None,
            "price_change_to_close_pct": None,
            "max_gain_after_first_rejection_pct": None,
            "would_have_been_profitable_to_close": None,
            "outcome_available": False,
            "missing_reason": "no_bars_after_first_rejection",
        }
    close_col = next((col for col in ("close", "c", "Close") if col in later.columns), None)
    high_col = next((col for col in ("high", "h", "High") if col in later.columns), None)
    if close_col is None:
        return {
            "first_rejection_price": None,
            "close_price": None,
            "max_high_after_first_rejection": None,
            "price_change_to_close_pct": None,
            "max_gain_after_first_rejection_pct": None,
            "would_have_been_profitable_to_close": None,
            "outcome_available": False,
            "missing_reason": "missing_close_column",
        }
    closes = pd.to_numeric(later[close_col], errors="coerce").dropna()
    if closes.empty:
        return {
            "first_rejection_price": None,
            "close_price": None,
            "max_high_after_first_rejection": None,
            "price_change_to_close_pct": None,
            "max_gain_after_first_rejection_pct": None,
            "would_have_been_profitable_to_close": None,
            "outcome_available": False,
            "missing_reason": "missing_close_values",
        }
    first_price = float(closes.iloc[0])
    close_price = float(closes.iloc[-1])
    highs = pd.to_numeric(later[high_col], errors="coerce").dropna() if high_col else closes
    max_high = float(highs.max()) if not highs.empty else close_price
    change = ((close_price / first_price) - 1.0) * 100.0 if first_price > 0 else None
    max_gain = ((max_high / first_price) - 1.0) * 100.0 if first_price > 0 else None
    return {
        "first_rejection_price": first_price,
        "close_price": close_price,
        "max_high_after_first_rejection": max_high,
        "price_change_to_close_pct": round(float(change), 4) if change is not None else None,
        "max_gain_after_first_rejection_pct": round(float(max_gain), 4) if max_gain is not None else None,
        "would_have_been_profitable_to_close": bool(change is not None and change > 0.0),
        "outcome_available": True,
        "missing_reason": None,
    }


def _parse_logs(*, day: str, paths: Sequence[Path]) -> tuple[dict[str, dict[str, Any]], list[str]]:
    rows: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "symbol": "",
            "first_rejection_time": None,
            "_first_dt": None,
            "last_rejection_time": None,
            "rejection_count": 0,
            "later_became_eligible": False,
            "later_entry_eval_time": None,
            "later_entry_eval_route": None,
            "later_entry_eval_reason": None,
            "last_rejection_reason": None,
        }
    )
    used: list[str] = []
    for path in paths:
        try:
            text = _read_text(path)
        except OSError:
            continue
        useful = False
        for line in text.splitlines():
            skip = _TREND_SKIP_RE.search(line)
            if skip:
                symbol = skip.group("symbol").upper()
                ts = _line_timestamp(line, day=day)
                row = rows[symbol]
                row["symbol"] = symbol
                row["rejection_count"] = int(row["rejection_count"]) + 1
                row["first_rejection_time"] = row["first_rejection_time"] or (ts.isoformat() if ts else None)
                row["_first_dt"] = row["_first_dt"] or ts
                row["last_rejection_time"] = ts.isoformat() if ts else row["last_rejection_time"]
                row["last_rejection_reason"] = skip.group("reason").strip()
                useful = True
                continue
            entry = _ENTRY_EVAL_RE.search(line)
            if entry:
                symbol = entry.group("symbol").upper()
                if symbol not in rows:
                    continue
                final = entry.group("final").strip().lower() in {"t", "true"}
                if final:
                    ts = _line_timestamp(line, day=day)
                    row = rows[symbol]
                    row["later_became_eligible"] = True
                    row["later_entry_eval_time"] = ts.isoformat() if ts else row["later_entry_eval_time"]
                    row["later_entry_eval_route"] = entry.group("route").strip()
                    row["later_entry_eval_reason"] = entry.group("reason").strip()
                    useful = True
        if useful:
            used.append(str(path))
    return rows, used


def build_trend_prefilter_research_report(
    *,
    project_root: Path | str = ".",
    data_dir: Path | str = "data",
    day: date | str,
    user_id: str = "live_bot",
    log_paths: Sequence[Path | str] | None = None,
    bars_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Build the research-only trend-prefilter report."""
    day_s = day.isoformat() if isinstance(day, date) else str(day)
    data = Path(data_dir)
    paths = discover_trend_prefilter_log_paths(project_root=project_root, day=day_s, extra_paths=log_paths)
    parsed, used_logs = _parse_logs(day=day_s, paths=paths)
    symbols: list[dict[str, Any]] = []
    for symbol, row in parsed.items():
        first_dt = row.get("_first_dt")
        outcome = (
            _outcome_from_bars(
                _load_local_bars_for_symbol(data_dir=data, bars_dir=bars_dir, symbol=symbol, day=day_s),
                first_rejection=first_dt,
                day=day_s,
            )
            if isinstance(first_dt, datetime)
            else {
                "first_rejection_price": None,
                "close_price": None,
                "max_high_after_first_rejection": None,
                "price_change_to_close_pct": None,
                "max_gain_after_first_rejection_pct": None,
                "would_have_been_profitable_to_close": None,
                "outcome_available": False,
                "missing_reason": "missing_first_rejection_timestamp",
            }
        )
        item = {key: value for key, value in row.items() if not key.startswith("_")}
        item.update(outcome)
        symbols.append(item)
    symbols.sort(key=lambda r: (-int(r.get("rejection_count") or 0), str(r.get("symbol") or "")))
    outcome_rows = [row for row in symbols if row.get("outcome_available")]
    profitable = [row for row in outcome_rows if row.get("would_have_been_profitable_to_close")]
    return {
        "version": 1,
        "date": day_s,
        "user_id": user_id,
        "research_only": True,
        "source_logs": used_logs,
        "summary": {
            "symbols": len(symbols),
            "total_rejections": sum(int(row.get("rejection_count") or 0) for row in symbols),
            "later_became_eligible": sum(1 for row in symbols if row.get("later_became_eligible")),
            "stayed_blocked_all_day": sum(1 for row in symbols if not row.get("later_became_eligible")),
            "outcomes_available": len(outcome_rows),
            "profitable_to_close_if_relaxed": len(profitable),
        },
        "symbols": symbols,
        "top_skipped_symbols": symbols[:25],
    }


def render_trend_prefilter_research_report(report: Mapping[str, Any]) -> str:
    summary = report.get("summary") if isinstance(report.get("summary"), Mapping) else {}
    lines = [
        f"Trend Prefilter Research - {report.get('date')} user={report.get('user_id')}",
        "Research-only. No trading behavior changed.",
        "",
        "Summary:",
        f"- symbols: {summary.get('symbols', 0)}",
        f"- total_rejections: {summary.get('total_rejections', 0)}",
        f"- later_became_eligible: {summary.get('later_became_eligible', 0)}",
        f"- stayed_blocked_all_day: {summary.get('stayed_blocked_all_day', 0)}",
        f"- outcomes_available: {summary.get('outcomes_available', 0)}",
        f"- profitable_to_close_if_relaxed: {summary.get('profitable_to_close_if_relaxed', 0)}",
        "",
        "Symbols:",
        "| symbol | first_rejection | rejects | later_eligible | close_return_pct | max_gain_pct | profitable_to_close | missing_reason |",
        "| --- | --- | ---: | --- | ---: | ---: | --- | --- |",
    ]
    for row in report.get("symbols") or []:
        close_ret = row.get("price_change_to_close_pct")
        max_gain = row.get("max_gain_after_first_rejection_pct")
        lines.append(
            "| %s | %s | %s | %s | %s | %s | %s | %s |"
            % (
                row.get("symbol"),
                row.get("first_rejection_time") or "n/a",
                row.get("rejection_count", 0),
                str(bool(row.get("later_became_eligible"))).lower(),
                "n/a" if close_ret is None else f"{float(close_ret):.2f}",
                "n/a" if max_gain is None else f"{float(max_gain):.2f}",
                "n/a" if row.get("would_have_been_profitable_to_close") is None else str(bool(row.get("would_have_been_profitable_to_close"))).lower(),
                row.get("missing_reason") or "",
            )
        )
    return "\n".join(lines) + "\n"


def trend_prefilter_paths(*, data_dir: Path | str, user_id: str, day: date | str) -> TrendPrefilterPaths:
    day_s = day.isoformat() if isinstance(day, date) else str(day)
    root = Path(data_dir) / "research" / "trend_prefilter_research"
    stem = f"{day_s}_{_safe_user(user_id)}"
    return TrendPrefilterPaths(json_path=root / f"{stem}.json", text_path=root / f"{stem}.txt")


def write_trend_prefilter_research_report(
    *,
    project_root: Path | str = ".",
    data_dir: Path | str = "data",
    day: date | str,
    user_id: str = "live_bot",
    log_paths: Sequence[Path | str] | None = None,
    bars_dir: Path | str | None = None,
) -> tuple[Path, Path, dict[str, Any]]:
    report = build_trend_prefilter_research_report(
        project_root=project_root,
        data_dir=data_dir,
        day=day,
        user_id=user_id,
        log_paths=log_paths,
        bars_dir=bars_dir,
    )
    paths = trend_prefilter_paths(data_dir=data_dir, user_id=user_id, day=day)
    paths.json_path.parent.mkdir(parents=True, exist_ok=True)
    paths.json_path.write_text(json.dumps(report, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    paths.text_path.write_text(render_trend_prefilter_research_report(report), encoding="utf-8")
    return paths.json_path, paths.text_path, report


__all__ = [
    "build_trend_prefilter_research_report",
    "discover_trend_prefilter_log_paths",
    "render_trend_prefilter_research_report",
    "trend_prefilter_paths",
    "write_trend_prefilter_research_report",
]
