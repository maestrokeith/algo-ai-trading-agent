"""Read-only trend-long loss review report."""

from __future__ import annotations

import gzip
import json
import math
import re
import subprocess
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from src.research_bars import _bar_timestamps, _read_bar_file, expected_bar_dirs
from src.trade_attribution import attribution_daily_path, load_daily_artifact

_ET = ZoneInfo("America/New_York")
_UTC = ZoneInfo("UTC")
_KV_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=([^ ]+)")
_ISO_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
_COMPACT_DATE_RE = re.compile(r"(\d{8})")
_SYSLOG_RE = re.compile(r"^(?P<mon>[A-Z][a-z]{2})\s+(?P<day>\d{1,2})\s+(?P<time>\d{2}:\d{2}:\d{2})\b")
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


def _day_text(day: date | str) -> str:
    return day.isoformat() if isinstance(day, date) else str(day)


def _compact_day(day: date | str) -> str:
    return _day_text(day).replace("-", "")


def _date_from_path(path: Path) -> str | None:
    for part in [path.name, *[parent.name for parent in path.parents]]:
        iso = _ISO_DATE_RE.search(part)
        if iso:
            return iso.group(1)
        compact = _COMPACT_DATE_RE.search(part)
        if compact:
            raw = compact.group(1)
            return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    return None


def _line_matches_day(line: str, day: str) -> bool:
    return day in line or _compact_day(day) in line or not re.search(r"\d{4}-\d{2}-\d{2}", line)


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


def _parse_kv(line: str) -> dict[str, str]:
    return {match.group(1): match.group(2).strip(",;") for match in _KV_RE.finditer(line)}


def _parse_timestamp(value: Any, *, day: str) -> datetime | None:
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value or "").strip()
        if not text:
            return None
        iso = re.search(r"\d{4}-\d{2}-\d{2}[T ][0-9:.:\-+Z]+", text)
        if iso:
            text = iso.group(0)
        else:
            syslog = _SYSLOG_RE.search(text)
            if syslog:
                expected = datetime.strptime(day, "%Y-%m-%d").date()
                month = _MONTHS.get(syslog.group("mon"))
                if month != expected.month or int(syslog.group("day")) != expected.day:
                    return None
                hh, mm, ss = (int(part) for part in syslog.group("time").split(":"))
                return datetime(expected.year, expected.month, expected.day, hh, mm, ss, tzinfo=_ET)
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    return dt.replace(tzinfo=_ET) if dt.tzinfo is None else dt.astimezone(_ET)


def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(_ET).isoformat() if dt is not None else None


def _read_text(path: Path) -> str:
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    return path.read_text(encoding="utf-8", errors="replace")


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
            ["journalctl", "-u", service, "--since", f"{day} 00:00:00", "--until", f"{day} 23:59:59", "--no-pager"],
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


def _symbol_from_line(line: str) -> str:
    kv = _parse_kv(line)
    symbol = str(kv.get("symbol") or kv.get("sym") or "").strip().upper()
    if symbol:
        return symbol
    match = re.search(r"\b(?:symbol|sym)=([A-Z][A-Z0-9.\-]{0,12})\b", line)
    return match.group(1).upper() if match else ""


def _is_trend_long(row: Mapping[str, Any]) -> bool:
    text = " ".join(str(row.get(key) or "") for key in ("route", "source", "entry_route", "entry_source", "strategy"))
    return "trend_long" in text.lower()


def _context_from_kv(kv: Mapping[str, Any]) -> dict[str, Any]:
    price = _round(kv.get("price") or kv.get("entry_price") or kv.get("paper_current_price") or kv.get("current_price"))
    vwap = _round(kv.get("vwap") or kv.get("session_vwap") or kv.get("paper_session_vwap"))
    out = {
        "trend": kv.get("trend") or kv.get("trend_direction") or kv.get("five_min_trend") or kv.get("trend_state"),
        "pullback": kv.get("pullback") or kv.get("pullback_state") or kv.get("pullback_ok"),
        "momentum": _round(kv.get("momentum") or kv.get("momentum_score") or kv.get("score") or kv.get("signal_score")),
        "relative_volume": _round(kv.get("relative_volume") or kv.get("rvol") or kv.get("rel_volume")),
        "spread_pct": _round(kv.get("spread_pct") or kv.get("spread")),
        "vwap": vwap,
        "ema20": _round(kv.get("ema20") or kv.get("ema_20")),
        "ema50": _round(kv.get("ema50") or kv.get("ema_50")),
        "entry_price": price,
    }
    if price is not None and vwap not in (None, 0):
        out["vwap_distance_pct"] = _round(((float(price) - float(vwap)) / float(vwap)) * 100.0)
    else:
        out["vwap_distance_pct"] = _round(kv.get("vwap_distance_pct") or kv.get("distance_from_vwap_pct"))
    return out


def _log_contexts(lines: Sequence[str], *, day: str) -> dict[str, list[dict[str, Any]]]:
    contexts: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for line in lines:
        if "trend_long" not in line.lower():
            continue
        if not any(marker in line for marker in ("ENTRY_ALIGNMENT_CONTEXT", "ENTRY_EVAL_PASS", "ORDER_SUBMITTED", "ORDER_FILLED")):
            continue
        symbol = _symbol_from_line(line)
        if not symbol:
            continue
        kv = _parse_kv(line)
        row = _context_from_kv(kv)
        row.update(
            {
                "symbol": symbol,
                "timestamp": _parse_timestamp(line, day=day),
                "raw_line": line,
            }
        )
        contexts[symbol].append(row)
    return contexts


def _nearest_prior_context(contexts: Mapping[str, Sequence[Mapping[str, Any]]], *, symbol: str, timestamp: datetime | None) -> dict[str, Any]:
    rows = [dict(row) for row in contexts.get(symbol, [])]
    if not rows:
        return {}
    if timestamp is None:
        return rows[-1]
    prior: list[tuple[float, dict[str, Any]]] = []
    for row in rows:
        ts = row.get("timestamp")
        if not isinstance(ts, datetime):
            continue
        delta = (timestamp - ts).total_seconds()
        if delta >= -1:
            prior.append((abs(delta), row))
    if prior:
        prior.sort(key=lambda item: item[0])
        return prior[0][1]
    return rows[-1]


def _attribution_rows(*, data_dir: Path, user_id: str, day: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    path = attribution_daily_path(data_dir=data_dir, user_id=user_id, day=day)
    payload = load_daily_artifact(path)
    orders = [dict(row) for row in payload.get("orders", []) if isinstance(row, Mapping)]
    exits = [dict(row) for row in payload.get("exits", []) if isinstance(row, Mapping)]
    return orders, exits


def _submitted_trend_entries(orders: Sequence[Mapping[str, Any]], *, day: str) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in orders:
        if str(row.get("action") or "").lower() != "buy" or not bool(row.get("submitted")):
            continue
        if not _is_trend_long(row):
            continue
        symbol = str(row.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        ts = _parse_timestamp(row.get("timestamp"), day=day)
        entry = {
            "symbol": symbol,
            "entry_time": _iso(ts),
            "_entry_dt": ts,
            "entry_price": _round(row.get("filled_avg_price") or row.get("entry_price") or row.get("price")),
            "qty": _round(row.get("filled_qty") or row.get("qty")),
            "route": row.get("route") or row.get("source") or "trend_long",
            "source": "trade_attribution",
            "raw_order": dict(row),
        }
        out[symbol].append(entry)
    for rows in out.values():
        rows.sort(key=lambda item: item.get("_entry_dt") or datetime.min.replace(tzinfo=_ET))
    return out


def _candidate_bar_files(data_dir: Path, symbol: str, day: str, bars_dir: Path | None) -> list[Path]:
    roots = [bars_dir] if bars_dir is not None else [*expected_bar_dirs(data_dir)]
    compact = day.replace("-", "")
    patterns = [
        f"**/{symbol}*{day}*.csv",
        f"**/{day}*{symbol}*.csv",
        f"**/{symbol}*{compact}*.csv",
        f"**/{compact}*{symbol}*.csv",
        f"**/{day}/**/{symbol}.csv",
        f"**/{compact}/**/{symbol}.csv",
    ]
    paths: list[Path] = []
    for root in roots:
        if root is None or not root.exists():
            continue
        for pattern in patterns:
            paths.extend(path for path in root.glob(pattern) if path.is_file())
    return sorted(dict.fromkeys(paths))


def _load_bars(data_dir: Path, *, symbol: str, day: str, bars_dir: Path | None, cache: dict[str, pd.DataFrame | None]) -> pd.DataFrame | None:
    if symbol in cache:
        return cache[symbol]
    frames: list[pd.DataFrame] = []
    for path in _candidate_bar_files(data_dir, symbol, day, bars_dir):
        _, frame = _read_bar_file(path)
        if frame is None or frame.empty or "close" not in frame.columns:
            continue
        ts = _bar_timestamps(frame)
        if ts is None:
            continue
        work = frame.copy()
        work["_timestamp"] = pd.to_datetime(ts, utc=True, errors="coerce")
        work = work[work["_timestamp"].notna()]
        if not work.empty:
            frames.append(work)
    if not frames:
        cache[symbol] = None
        return None
    bars = pd.concat(frames, ignore_index=True).sort_values("_timestamp").drop_duplicates(subset=["_timestamp"], keep="last")
    day_mask = bars["_timestamp"].dt.tz_convert(_ET).dt.date.astype(str) == day
    bars = bars.loc[day_mask].reset_index(drop=True)
    cache[symbol] = bars if not bars.empty else None
    return cache[symbol]


def _mfe_mae(
    *,
    data_dir: Path,
    symbol: str,
    day: str,
    entry_dt: datetime | None,
    exit_dt: datetime | None,
    entry_price: float | None,
    bars_dir: Path | None,
    cache: dict[str, pd.DataFrame | None],
) -> tuple[float | None, float | None]:
    if entry_dt is None or exit_dt is None or entry_price is None or entry_price <= 0:
        return None, None
    bars = _load_bars(data_dir, symbol=symbol, day=day, bars_dir=bars_dir, cache=cache)
    if bars is None or bars.empty:
        return None, None
    window = bars.loc[
        (bars["_timestamp"] >= pd.Timestamp(entry_dt.astimezone(_UTC)))
        & (bars["_timestamp"] <= pd.Timestamp(exit_dt.astimezone(_UTC)))
    ]
    if window.empty:
        return None, None
    high = _safe_float(window["high"].max()) if "high" in window.columns else _safe_float(window["close"].max())
    low = _safe_float(window["low"].min()) if "low" in window.columns else _safe_float(window["close"].min())
    mfe = _round(((high - entry_price) / entry_price) * 100.0) if high is not None else None
    mae = _round(((low - entry_price) / entry_price) * 100.0) if low is not None else None
    return mfe, mae


def _first_close_at_or_after(bars: pd.DataFrame, ts: datetime) -> float | None:
    matches = bars.loc[bars["_timestamp"] >= pd.Timestamp(ts.astimezone(_UTC))]
    if matches.empty:
        return None
    return _safe_float(matches.iloc[0].get("close"))


def _forward_return_pct(
    *,
    data_dir: Path,
    symbol: str,
    day: str,
    ts: datetime | None,
    bars_dir: Path | None,
    cache: dict[str, pd.DataFrame | None],
    minutes: int = 15,
) -> float | None:
    if ts is None:
        return None
    bars = _load_bars(data_dir, symbol=symbol, day=day, bars_dir=bars_dir, cache=cache)
    if bars is None or bars.empty:
        return None
    entry = _first_close_at_or_after(bars, ts)
    future = _first_close_at_or_after(bars, ts + timedelta(minutes=minutes))
    if entry is None or future is None or entry <= 0:
        return None
    return _round(((future - entry) / entry) * 100.0)


def _fallback_entry(symbol: str, exit_dt: datetime | None, contexts: Mapping[str, Sequence[Mapping[str, Any]]]) -> dict[str, Any]:
    context = _nearest_prior_context(contexts, symbol=symbol, timestamp=exit_dt)
    ts = context.get("timestamp") if isinstance(context.get("timestamp"), datetime) else None
    return {
        "symbol": symbol,
        "entry_time": _iso(ts),
        "_entry_dt": ts,
        "entry_price": _round(context.get("entry_price")),
        "qty": None,
        "route": "trend_long",
        "source": "logs",
    }


def _review_rows(
    *,
    orders: Sequence[Mapping[str, Any]],
    exits: Sequence[Mapping[str, Any]],
    contexts: Mapping[str, Sequence[Mapping[str, Any]]],
    data_dir: Path,
    day: str,
    bars_dir: Path | None,
) -> list[dict[str, Any]]:
    entries = _submitted_trend_entries(orders, day=day)
    used_entries: set[tuple[str, int]] = set()
    cache: dict[str, pd.DataFrame | None] = {}
    rows: list[dict[str, Any]] = []
    reentry_counts = Counter()
    for symbol, symbol_entries in entries.items():
        if len(symbol_entries) > 1:
            reentry_counts[symbol] = len(symbol_entries) - 1
    for exit_row in exits:
        if not _is_trend_long(exit_row):
            continue
        symbol = str(exit_row.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        exit_dt = _parse_timestamp(exit_row.get("timestamp"), day=day)
        selected_idx: int | None = None
        selected_entry: dict[str, Any] | None = None
        for idx, entry in enumerate(entries.get(symbol, [])):
            if (symbol, idx) in used_entries:
                continue
            entry_dt = entry.get("_entry_dt")
            if isinstance(entry_dt, datetime) and exit_dt is not None and entry_dt > exit_dt:
                continue
            selected_idx = idx
            selected_entry = dict(entry)
            break
        if selected_entry is None:
            selected_entry = _fallback_entry(symbol, exit_dt, contexts)
        elif selected_idx is not None:
            used_entries.add((symbol, selected_idx))
        context = _nearest_prior_context(contexts, symbol=symbol, timestamp=selected_entry.get("_entry_dt") if isinstance(selected_entry.get("_entry_dt"), datetime) else exit_dt)
        entry_price = _safe_float(selected_entry.get("entry_price")) or _safe_float(exit_row.get("entry_price"))
        exit_price = _safe_float(exit_row.get("exit_price") or exit_row.get("filled_avg_price") or exit_row.get("price"))
        qty = _safe_float(exit_row.get("qty")) or _safe_float(selected_entry.get("qty"))
        pnl = _safe_float(exit_row.get("pnl"))
        if pnl is None and entry_price is not None and exit_price is not None and qty is not None:
            pnl = (exit_price - entry_price) * qty
        pnl_pct = _safe_float(exit_row.get("pnl_pct"))
        if pnl_pct is None and entry_price not in (None, 0) and exit_price is not None:
            pnl_pct = ((exit_price - entry_price) / entry_price) * 100.0
        mfe, mae = _mfe_mae(
            data_dir=data_dir,
            symbol=symbol,
            day=day,
            entry_dt=selected_entry.get("_entry_dt") if isinstance(selected_entry.get("_entry_dt"), datetime) else None,
            exit_dt=exit_dt,
            entry_price=entry_price,
            bars_dir=bars_dir,
            cache=cache,
        )
        row = {
            "symbol": symbol,
            "entry_time": selected_entry.get("entry_time"),
            "exit_time": _iso(exit_dt),
            "entry_price": _round(entry_price),
            "exit_price": _round(exit_price),
            "qty": _round(qty),
            "pnl": _round(pnl),
            "pnl_pct": _round(pnl_pct),
            "exit_reason": exit_row.get("exit_reason") or exit_row.get("reason") or "unknown",
            "stop_distance_pct": _round(exit_row.get("stop_distance_pct") or exit_row.get("stop_pct")),
            "max_favorable_excursion_pct": mfe,
            "max_adverse_excursion_pct": mae,
            "reentry_happened": reentry_counts[symbol] > 0,
            "reentry_count": int(reentry_counts[symbol]),
            "churn_reversal_count": int(reentry_counts[symbol] + (1 if (_safe_float(exit_row.get("hold_minutes")) or 9999) < 30 else 0)),
            "hold_minutes": _round(exit_row.get("hold_minutes")),
            "entry_context": {
                "trend": context.get("trend"),
                "pullback": context.get("pullback"),
                "momentum": _round(context.get("momentum")),
                "relative_volume": _round(context.get("relative_volume")),
                "spread_pct": _round(context.get("spread_pct")),
                "vwap": _round(context.get("vwap")),
                "vwap_distance_pct": _round(context.get("vwap_distance_pct")),
                "ema20": _round(context.get("ema20")),
                "ema50": _round(context.get("ema50")),
            },
        }
        rows.append(row)
    return sorted(rows, key=lambda row: (str(row.get("exit_time") or ""), str(row.get("symbol") or "")))


def _reentry_analysis(
    *,
    orders: Sequence[Mapping[str, Any]],
    exits: Sequence[Mapping[str, Any]],
    data_dir: Path,
    day: str,
    bars_dir: Path | None,
    cooldown_minutes: float = 90.0,
) -> dict[str, Any]:
    entries = _submitted_trend_entries(orders, day=day)
    stop_rows: list[dict[str, Any]] = []
    for row in exits:
        if not _is_trend_long(row):
            continue
        reason = str(row.get("exit_reason") or row.get("reason") or "").lower()
        if "stop" not in reason:
            continue
        symbol = str(row.get("symbol") or "").strip().upper()
        stop_dt = _parse_timestamp(row.get("timestamp"), day=day)
        if not symbol or stop_dt is None:
            continue
        stop_rows.append({"symbol": symbol, "stop_time": _iso(stop_dt), "_stop_dt": stop_dt, "pnl": _round(row.get("pnl"))})

    cache: dict[str, pd.DataFrame | None] = {}
    analysis: list[dict[str, Any]] = []
    estimated_avoided_loss = 0.0
    estimated_missed_gain = 0.0
    for stop in stop_rows:
        symbol = str(stop["symbol"])
        stop_dt = stop["_stop_dt"]
        attempted = next(
            (
                entry
                for entry in entries.get(symbol, [])
                if isinstance(entry.get("_entry_dt"), datetime) and entry["_entry_dt"] > stop_dt
            ),
            None,
        )
        attempted_dt = attempted.get("_entry_dt") if isinstance(attempted, Mapping) else None
        age = (attempted_dt - stop_dt).total_seconds() / 60.0 if isinstance(attempted_dt, datetime) else None
        cooldown_remaining = max(0.0, float(cooldown_minutes) - float(age or 0.0)) if age is not None else None
        would_block = bool(age is not None and cooldown_remaining is not None and cooldown_remaining > 1e-9)
        later_loss = 0.0
        if isinstance(attempted_dt, datetime):
            for exit_row in exits:
                if not _is_trend_long(exit_row):
                    continue
                if str(exit_row.get("symbol") or "").strip().upper() != symbol:
                    continue
                exit_dt = _parse_timestamp(exit_row.get("timestamp"), day=day)
                if exit_dt is None or exit_dt <= attempted_dt:
                    continue
                pnl = _safe_float(exit_row.get("pnl"))
                if pnl is not None and pnl < 0:
                    later_loss = abs(pnl)
                    break
        fwd = _forward_return_pct(data_dir=data_dir, symbol=symbol, day=day, ts=attempted_dt, bars_dir=bars_dir, cache=cache)
        if would_block and later_loss > 0:
            estimated_avoided_loss += later_loss
        if would_block and fwd is not None and fwd > 0:
            estimated_missed_gain += fwd
        analysis.append(
            {
                "symbol": symbol,
                "stop_time": stop["stop_time"],
                "attempted_reentry": _iso(attempted_dt) if isinstance(attempted_dt, datetime) else None,
                "cooldown_remaining_minutes": _round(cooldown_remaining),
                "would_have_been_blocked": would_block,
                "forward_return_if_blocked_pct": fwd,
                "estimated_avoided_loss": _round(later_loss if would_block else 0.0),
                "estimated_missed_gain_pct": _round(fwd if would_block and fwd is not None and fwd > 0 else 0.0),
                "recommendation": "block_reentry_after_stop" if would_block else "allow_after_cooldown_or_no_attempt",
            }
        )
    return {
        "rows": analysis,
        "summary": {
            "estimated_avoided_loss": _round(estimated_avoided_loss),
            "estimated_missed_gain": _round(estimated_missed_gain),
            "net_benefit": _round(estimated_avoided_loss - estimated_missed_gain),
        },
    }


def _recommendations(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    recs: list[str] = []
    losing = [row for row in rows if (_safe_float(row.get("pnl")) or 0.0) < 0.0 or (_safe_float(row.get("pnl_pct")) or 0.0) < 0.0]
    if not rows:
        return ["No trend_long exits found for the requested day; verify attribution/log coverage before tuning."]
    churn = [row for row in losing if int(row.get("churn_reversal_count") or 0) > 0]
    stop_losses = [row for row in losing if "stop" in str(row.get("exit_reason") or "").lower()]
    adverse = [row for row in losing if (_safe_float(row.get("max_adverse_excursion_pct")) or 0.0) < -2.0]
    if churn:
        recs.append("Review trend_long re-entry/churn controls for symbols with short-hold losses or same-day re-entry.")
    if stop_losses:
        recs.append("Review stop placement versus entry context for losses exited by stop-related reasons.")
    if adverse:
        recs.append("Inspect symbols with large adverse excursion before exit; stops may be reacting late or entries may be late.")
    if not recs:
        recs.append("Losses do not show a dominant churn/stop signature in available data; inspect missing context fields before tuning.")
    return recs


def build_trend_long_loss_review(
    *,
    project_root: Path | str = ".",
    data_dir: Path | str = "data",
    day: date | str,
    user_id: str,
    bars_dir: Path | str | None = None,
    log_text: str | None = None,
    log_files: Sequence[Path | str] | None = None,
) -> dict[str, Any]:
    """Build a read-only trend_long realized-loss review."""
    day_s = _day_text(day)
    root = Path(project_root)
    data = Path(data_dir)
    bars_path = Path(bars_dir) if bars_dir is not None else None
    lines = _load_log_lines(project_root=root, data_dir=data, day=day_s, log_text=log_text, log_files=log_files)
    orders, exits = _attribution_rows(data_dir=data, user_id=user_id, day=day_s)
    contexts = _log_contexts(lines, day=day_s)
    rows = _review_rows(orders=orders, exits=exits, contexts=contexts, data_dir=data, day=day_s, bars_dir=bars_path)
    reentry = _reentry_analysis(orders=orders, exits=exits, data_dir=data, day=day_s, bars_dir=bars_path)
    losses = [row for row in rows if (_safe_float(row.get("pnl")) or 0.0) < 0.0 or (_safe_float(row.get("pnl_pct")) or 0.0) < 0.0]
    top_losers = sorted(losses, key=lambda row: (_safe_float(row.get("pnl")) if _safe_float(row.get("pnl")) is not None else _safe_float(row.get("pnl_pct")) or 0.0))[:10]
    return {
        "report": "trend_long_loss_review",
        "research_only": True,
        "date": day_s,
        "user_id": str(user_id or "default"),
        "summary": {
            "trend_long_exits": len(rows),
            "losing_trades": len(losses),
            "total_pnl": _round(sum(_safe_float(row.get("pnl")) or 0.0 for row in rows)),
            "total_loss_pnl": _round(sum(_safe_float(row.get("pnl")) or 0.0 for row in losses)),
            "exit_reasons": dict(Counter(str(row.get("exit_reason") or "unknown") for row in rows)),
            "reentry_symbols": sorted({str(row.get("symbol")) for row in rows if row.get("reentry_happened")}),
        },
        "trend_long_trades": rows,
        "reentry_analysis": reentry,
        "top_losing_symbols": top_losers,
        "recommendations": _recommendations(rows),
        "debug": {
            "log_lines_read": len(lines),
            "attribution_orders": len(orders),
            "attribution_exits": len(exits),
            "context_symbols": sorted(contexts.keys()),
        },
    }


def render_trend_long_loss_review(report: Mapping[str, Any]) -> str:
    """Render trend_long loss review as Markdown."""
    summary = report.get("summary") if isinstance(report.get("summary"), Mapping) else {}
    lines = [
        f"# Trend Long Loss Review {report.get('date')} user={report.get('user_id')}",
        "",
        "Read-only research: no trading behavior, thresholds, orders, exits, stops, or sizing changed.",
        "",
        "## Summary",
        f"- trend_long exits: {summary.get('trend_long_exits', 0)}",
        f"- losing trades: {summary.get('losing_trades', 0)}",
        f"- total pnl: {summary.get('total_pnl')}",
        f"- total loss pnl: {summary.get('total_loss_pnl')}",
        f"- exit reasons: {summary.get('exit_reasons') or {}}",
        f"- re-entry symbols: {', '.join(summary.get('reentry_symbols') or []) if summary.get('reentry_symbols') else 'none'}",
        "",
        "## Trend Long Entries / Exits",
        "| Symbol | Entry | Exit | Entry $ | Exit $ | PnL | PnL % | Exit Reason | Stop Dist % | MFE % | MAE % | Re-entry | Churn |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: | --- | ---: | ---: | ---: | --- | ---: |",
    ]
    rows = report.get("trend_long_trades") if isinstance(report.get("trend_long_trades"), list) else []
    if not rows:
        lines.append("| none | | | | | | | | | | | | |")
    for row in rows:
        lines.append(
            f"| {row.get('symbol')} | {row.get('entry_time') or 'n/a'} | {row.get('exit_time') or 'n/a'} | "
            f"{row.get('entry_price')} | {row.get('exit_price')} | {row.get('pnl')} | {row.get('pnl_pct')} | "
            f"{row.get('exit_reason')} | {row.get('stop_distance_pct')} | {row.get('max_favorable_excursion_pct')} | "
            f"{row.get('max_adverse_excursion_pct')} | {row.get('reentry_happened')} | {row.get('churn_reversal_count')} |"
        )
    lines.extend(["", "## Entry Context"])
    for row in rows:
        ctx = row.get("entry_context") if isinstance(row.get("entry_context"), Mapping) else {}
        lines.append(
            f"- {row.get('symbol')}: trend={ctx.get('trend')} pullback={ctx.get('pullback')} "
            f"momentum={ctx.get('momentum')} rvol={ctx.get('relative_volume')} spread={ctx.get('spread_pct')} "
            f"vwap={ctx.get('vwap')} vwap_distance={ctx.get('vwap_distance_pct')} "
            f"ema20={ctx.get('ema20')} ema50={ctx.get('ema50')}"
        )
    reentry = report.get("reentry_analysis") if isinstance(report.get("reentry_analysis"), Mapping) else {}
    reentry_summary = reentry.get("summary") if isinstance(reentry.get("summary"), Mapping) else {}
    reentry_rows = reentry.get("rows") if isinstance(reentry.get("rows"), list) else []
    lines.extend(
        [
            "",
            "## Re-entry Analysis",
            f"- estimated avoided loss: {reentry_summary.get('estimated_avoided_loss')}",
            f"- estimated missed gain: {reentry_summary.get('estimated_missed_gain')}",
            f"- net benefit: {reentry_summary.get('net_benefit')}",
            "",
            "| Symbol | Stop Time | Attempted Re-entry | Cooldown Remaining | Would Block | Forward Return % | Recommendation |",
            "| --- | --- | --- | ---: | --- | ---: | --- |",
        ]
    )
    if not reentry_rows:
        lines.append("| none | | | | | | |")
    for row in reentry_rows:
        lines.append(
            f"| {row.get('symbol')} | {row.get('stop_time')} | {row.get('attempted_reentry') or 'none'} | "
            f"{row.get('cooldown_remaining_minutes')} | {row.get('would_have_been_blocked')} | "
            f"{row.get('forward_return_if_blocked_pct')} | {row.get('recommendation')} |"
        )
    lines.extend(["", "## Top Losing Symbols"])
    top = report.get("top_losing_symbols") if isinstance(report.get("top_losing_symbols"), list) else []
    if not top:
        lines.append("- none")
    for row in top:
        lines.append(f"- {row.get('symbol')}: pnl={row.get('pnl')} pnl_pct={row.get('pnl_pct')} exit_reason={row.get('exit_reason')}")
    lines.extend(["", "## Recommendations"])
    for rec in report.get("recommendations") or []:
        lines.append(f"- {rec}")
    return "\n".join(lines).rstrip() + "\n"


def write_trend_long_loss_review(
    *,
    project_root: Path | str = ".",
    data_dir: Path | str = "data",
    day: date | str,
    user_id: str,
    bars_dir: Path | str | None = None,
    log_text: str | None = None,
    log_files: Sequence[Path | str] | None = None,
) -> tuple[Path, Path, dict[str, Any]]:
    """Build and write trend_long loss review artifacts."""
    data = Path(data_dir)
    day_s = _day_text(day)
    report = build_trend_long_loss_review(
        project_root=project_root,
        data_dir=data,
        day=day_s,
        user_id=user_id,
        bars_dir=bars_dir,
        log_text=log_text,
        log_files=log_files,
    )
    out_dir = data / "research_metrics" / day_s
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "trend_long_loss_review.json"
    text_path = out_dir / "trend_long_loss_review.md"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    text_path.write_text(render_trend_long_loss_review(report), encoding="utf-8")
    return json_path, text_path, report


__all__ = [
    "build_trend_long_loss_review",
    "render_trend_long_loss_review",
    "write_trend_long_loss_review",
]
