from __future__ import annotations

import ast
import gzip
import json
import re
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd


_COMPACT_DATE_RE = re.compile(r"(\d{8})")
_ISO_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
_KV_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=([^ ]+)")
_ET = ZoneInfo("America/New_York")
_UTC = ZoneInfo("UTC")
_ENTRY_ALIGNMENT_BREAKOUT_TEXT = (
    "need 5m breakout OR new intraday high OR strong green 1m OR opening-range breakout"
)


def _safe_user(user_id: str) -> str:
    return "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in str(user_id or "default"))


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_float_or_none(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out else None


def _parse_timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        ts = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=_ET)
    return ts


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


def _parse_kv(line: str) -> dict[str, str]:
    return {match.group(1): match.group(2).strip(",") for match in _KV_RE.finditer(line)}


def _symbol_from_line(line: str, kv: Mapping[str, str]) -> str:
    raw = kv.get("symbol") or kv.get("sym") or kv.get("sym_u")
    if raw:
        return str(raw).strip().upper().strip(",")
    match = re.search(r"\b([A-Z][A-Z0-9.\-]{0,9})\s+ENTRY_EVAL\b", line)
    if match:
        return match.group(1).upper()
    match = re.search(r"\bALLOCATOR_REJECT\s+([A-Z][A-Z0-9.\-]{0,9})\b", line)
    if match:
        return match.group(1).upper()
    match = re.search(r"\bDYNAMIC_SCAN reject\s+([A-Z][A-Z0-9.\-]{0,9})\b", line)
    if match:
        return match.group(1).upper()
    return ""


def _reason_from_line(line: str, kv: Mapping[str, str]) -> str:
    reason_match = re.search(r"\breason=(.+?)(?=\s+[A-Za-z_][A-Za-z0-9_]*=|$)", line)
    if reason_match:
        return reason_match.group(1).strip()
    match = re.search(r"reason=([^ ]+)", line)
    if match:
        return match.group(1).strip()
    if "not enough bars" in line:
        return "short_history"
    if "spread" in line.lower() and "cap" in line.lower():
        return "spread_cap"
    match = re.search(r"\bDYNAMIC_SCAN reject [A-Z][A-Z0-9.\-]{0,9}:\s*(.+)$", line)
    if match:
        return match.group(1).strip()
    return "unknown"


def normalize_gate_reason(reason: str | None, line: str = "") -> str:
    """Map log-specific reasons to stable research buckets."""
    text = f"{reason or ''} {line or ''}".lower()
    if "no_catalyst" in text:
        return "no_catalyst"
    if "no_decision" in text:
        return "no_decision"
    if "bad quote" in text or "bad_quote" in text or "invalid_quote" in text or "no quote" in text:
        return "bad_quote"
    if "not enough bars" in text or "short_history" in text or "200-bar" in text or "ma200" in text:
        return "short_history"
    if "unstable quote" in text or "dynamic unstable quote" in text:
        return "unstable_quote"
    if "below_min_price" in text or "price below minimum" in text or "below minimum price" in text:
        return "below_min_price"
    if "spread cap" in text or "spread_cap" in text or "spread too wide" in text or "dynamic spread" in text:
        return "spread_cap"
    if "entry_alignment" in text or "entry alignment" in text:
        return "entry_alignment"
    if "vwap extension" in text or "vwap_extension" in text or "dynamic vwap" in text or "not above vwap" in text:
        return "vwap_extension"
    if "relative_volume" in text or "relative volume" in text or "rel_volume" in text:
        return "relative_volume"
    if "gain filter" in text or "above_max_day_gain" in text or "excessive gain" in text:
        return "excessive_gain"
    if "below_min_day_gain" in text:
        return "entry_alignment"
    if "trade_cycle" in text or "no_trade_cycle_allowed" in text:
        return "trade_cycle"
    if "min notional" in text or "min_order_notional" in text or "min_realloc_leg" in text:
        return "min_notional"
    if "gross_headroom" in text or "gross cap" in text or "gross_headroom" in text:
        return "gross_headroom"
    if "cash reserve" in text or "cash_reserve" in text or "available_cash" in text:
        return "cash_reserve"
    if "dynamic_sleeve" in text or "sleeve cap" in text:
        return "sleeve_cap"
    if "max positions" in text or "max_positions" in text:
        return "max_positions"
    if "same-day" in text or "same_day" in text or "post_sell" in text or "post-sell" in text:
        return "same_day_sold_guard"
    if "prefilter" in text or "dynamic_not_tradable" in text:
        return "prefilter_reject"
    if "entry_eval" in text and ("final=f" in text or "final=false" in text or "final=0" in text):
        return "entry_eval_reject"
    if "no entry signal" in text or "entry_gates_blocked" in text:
        return "entry_eval_reject"
    if "allocator" in text:
        return "allocator_reject"
    return str(reason or "unknown").strip() or "unknown"


def downstream_gate_category(reason: str | None, line: str = "", stage: str | None = None) -> str:
    """Map downstream terminal records to stable report-only attribution buckets."""
    text = f"{reason or ''} {line or ''} {stage or ''}".lower()
    if "entry_eval" in text or "entry_alignment" in text or "no_decision" in text:
        return "entry_eval_reject"
    if "no_catalyst" in text:
        return "allocator_filtered_no_catalyst"
    if "rank_cap" in text or "rank cap" in text or "ranked_out" in text or "not_selected" in text:
        return "allocator_filtered_rank_cap"
    if "dynamic_relative_volume" in text or "relative_volume" in text or "relative volume" in text:
        return "dispatch_dynamic_relative_volume"
    if "dynamic_vwap" in text or "vwap" in text:
        return "dispatch_dynamic_vwap"
    if "no_quote" in text or "no quote" in text or "bad quote" in text or "invalid_quote" in text:
        return "no_quote"
    if (
        "missing_outcome" in text
        or "missing_terminal" in text
        or "legacy_missing_terminal" in text
        or "not_reached" in text
        or "no_allocator_action" in text
        or "not_seen_by_allocator" in text
    ):
        return "missing_terminal_record"
    return normalize_gate_reason(reason, line)


@dataclass
class SymbolGateState:
    symbol: str
    dynamic_score: float = 0.0
    observed_at: str | None = None
    observed_price: float | None = None
    source_path: str | None = None
    seen_dynamic: bool = False
    selected_dynamic: bool = False
    entry_eval_passed: bool = False
    became_order: bool = False
    allocator_action_created: bool = False
    final_rejection_reason: str | None = None
    final_gate: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)

    def note(self, *, event: str, reason: str | None = None, line: str | None = None, score: Any = None) -> None:
        if score is not None:
            self.dynamic_score = max(self.dynamic_score, _safe_float(score))
        gate = normalize_gate_reason(reason, line or "") if reason else None
        if reason:
            self.final_rejection_reason = reason
        if gate:
            self.final_gate = gate
        self.events.append({"event": event, "reason": reason, "gate": gate, "line": line})


def _state(states: dict[str, SymbolGateState], symbol: str) -> SymbolGateState:
    sym = str(symbol or "").strip().upper()
    if sym not in states:
        states[sym] = SymbolGateState(symbol=sym)
    return states[sym]


def _parse_selected_list(line: str) -> list[str]:
    match = re.search(r"DYNAMIC_SCAN selected=(\[.*?\])", line)
    if not match:
        return []
    try:
        parsed = ast.literal_eval(match.group(1))
    except (SyntaxError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item).strip().upper() for item in parsed if str(item).strip()]


def _read_text(path: Path) -> str:
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    return path.read_text(encoding="utf-8", errors="replace")


def discover_dynamic_gate_log_paths(
    *,
    project_root: Path | str = ".",
    day: str,
    extra_paths: Sequence[Path | str] | None = None,
) -> list[Path]:
    root = Path(project_root)
    paths: list[Path] = []
    for pattern_root, patterns in (
        (root / "data" / "logs", [f"*{day}*.log", f"*{day}*.txt"]),
        (root / "reports" / "debug", ["algo_debug_*.log", "algo_debug_*.log.gz"]),
        (root / "logs", [f"*{day}*.log"]),
    ):
        if not pattern_root.exists():
            continue
        for pattern in patterns:
            for path in pattern_root.glob(pattern):
                if path.is_file() and (_date_from_path(path) in {None, day} or day in path.name):
                    paths.append(path)
    for extra in extra_paths or []:
        path = Path(extra)
        if path.exists() and path.is_file():
            paths.append(path)
    return sorted(dict.fromkeys(paths))


def _history_paths(data_dir: Path, *, day: str, user_id: str) -> list[Path]:
    root = data_dir / "dynamic_scan_history"
    if not root.exists():
        return []
    safe_user = _safe_user(user_id)
    paths = []
    for path in root.glob("*.json"):
        if _date_from_path(path) != day:
            continue
        if path.name.endswith(f"_{safe_user}.json") or path.name.endswith("_default.json"):
            paths.append(path)
    return sorted(paths)


def _resolve_replay_path(data_dir: Path, raw_path: Any) -> Path | None:
    text = str(raw_path or "").strip()
    if not text:
        return None
    path = Path(text)
    if path.is_absolute() and path.exists():
        return path
    for candidate in (Path.cwd() / path, data_dir.parent / path, data_dir / path):
        if candidate.exists():
            return candidate
    return None


def _replay_market_session_paths(data_dir: Path, *, day: str, user_id: str) -> list[Path]:
    safe = _safe_user(user_id)
    candidates = [
        data_dir / "replay_market_session" / f"{day}_{safe}.json",
        data_dir / "replay_market_session" / "_cycles" / f"{day}_{safe}.json",
    ]
    return [path for path in candidates if path.exists()]


def _replay_history_paths(data_dir: Path, *, day: str, user_id: str) -> list[Path]:
    paths: list[Path] = []
    for replay_path in _replay_market_session_paths(data_dir, day=day, user_id=user_id):
        try:
            payload = json.loads(replay_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for cycle in payload.get("cycle_summaries") or []:
            if not isinstance(cycle, Mapping):
                continue
            resolved = _resolve_replay_path(data_dir, cycle.get("history_path"))
            if resolved is not None and resolved.is_file():
                paths.append(resolved)
        resolved = _resolve_replay_path(data_dir, payload.get("history_path"))
        if resolved is not None and resolved.is_file():
            paths.append(resolved)
    return sorted(dict.fromkeys(paths))


def _bar_timestamps_utc(bars: pd.DataFrame) -> pd.Series | None:
    if bars.empty:
        return None
    if isinstance(bars.index, pd.DatetimeIndex):
        values = pd.Series(bars.index, index=bars.index)
    else:
        values = None
        for col in ("timestamp", "datetime", "time", "ts", "t"):
            if col in bars.columns:
                values = bars[col]
                break
        if values is None:
            return None
    parsed = pd.to_datetime(values, utc=True, errors="coerce")
    if parsed.isna().all():
        return None
    return pd.Series(parsed, index=bars.index)


def _local_bar_roots(data_dir: Path, bars_dir: Path | str | None) -> list[Path]:
    if bars_dir is not None:
        return [Path(bars_dir)]
    return [
        data_dir / "research" / "dynamic_candidate_bars",
        data_dir / "research" / "allocator_candidate_bars",
        data_dir / "historical_bars",
        data_dir / "bars",
        data_dir / "market",
        data_dir / "market_bars",
        data_dir / "intraday_bars",
        data_dir / "intraday_snapshots",
        data_dir / "snapshots",
        data_dir / "alpaca_cache",
        data_dir / "cache" / "alpaca",
        data_dir / "replay_market_session",
        data_dir / "replay",
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


def _close_forward_return_from_bars(
    bars: pd.DataFrame | None,
    *,
    observed_at: datetime,
    observed_price: float,
    minutes: int | None,
) -> float | None:
    if bars is None or bars.empty or observed_price <= 0:
        return None
    close_col = next((col for col in ("close", "Close", "c") if col in bars.columns), None)
    if close_col is None:
        return None
    timestamps = _bar_timestamps_utc(bars)
    if timestamps is None:
        return None
    observed_utc = observed_at.astimezone(_UTC)
    observed_day = observed_utc.astimezone(_ET).date()
    day_mask = timestamps.dt.tz_convert(_ET).dt.date == observed_day
    day_bars = bars.loc[day_mask].copy()
    if day_bars.empty:
        return None
    day_ts = timestamps.loc[day_bars.index]
    if minutes is None:
        eligible = day_bars.loc[day_ts > observed_utc]
    else:
        target = observed_utc + pd.Timedelta(minutes=int(minutes))
        eligible = day_bars.loc[day_ts >= target]
    if eligible.empty:
        return None
    if minutes is None:
        value = pd.to_numeric(eligible[close_col], errors="coerce").dropna()
        if value.empty:
            return None
        close = float(value.iloc[-1])
    else:
        value = pd.to_numeric(eligible[close_col], errors="coerce").dropna()
        if value.empty:
            return None
        close = float(value.iloc[0])
    return ((close / float(observed_price)) - 1.0) * 100.0


def _is_target_entry_alignment_reject(st: SymbolGateState) -> bool:
    text = str(st.final_rejection_reason or "")
    return (
        st.final_gate == "entry_alignment"
        and _ENTRY_ALIGNMENT_BREAKOUT_TEXT.lower() in text.lower()
    )


def _mean(values: Sequence[float]) -> float | None:
    if not values:
        return None
    return sum(values) / float(len(values))


def _median(values: Sequence[float]) -> float | None:
    if not values:
        return None
    sorted_values = sorted(values)
    mid = len(sorted_values) // 2
    if len(sorted_values) % 2:
        return sorted_values[mid]
    return (sorted_values[mid - 1] + sorted_values[mid]) / 2.0


def _return_summary(rows: Sequence[Mapping[str, Any]], key: str) -> dict[str, Any]:
    values = [
        float(row[key])
        for row in rows
        if row.get(key) is not None
    ]
    if not values:
        return {
            "count": 0,
            "average_return_pct": None,
            "median_return_pct": None,
            "win_rate": None,
            "best_examples": [],
            "worst_examples": [],
        }
    with_values = [row for row in rows if row.get(key) is not None]
    return {
        "count": len(values),
        "average_return_pct": _mean(values),
        "median_return_pct": _median(values),
        "win_rate": sum(1 for value in values if value > 0.0) / float(len(values)),
        "best_examples": sorted(
            with_values,
            key=lambda row: float(row.get(key) or 0.0),
            reverse=True,
        )[:5],
        "worst_examples": sorted(
            with_values,
            key=lambda row: float(row.get(key) or 0.0),
        )[:5],
    }


def _entry_alignment_forward_return_research(
    states: Mapping[str, SymbolGateState],
    *,
    data_dir: Path,
    day: str,
    bars_dir: Path | str | None = None,
) -> dict[str, Any]:
    rejects = [st for st in states.values() if _is_target_entry_alignment_reject(st)]
    bars_cache: dict[str, pd.DataFrame | None] = {}
    rows: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    for st in sorted(rejects, key=lambda item: item.symbol):
        observed_at = _parse_timestamp(st.observed_at)
        observed_price = st.observed_price
        if observed_at is None:
            missing.append({"symbol": st.symbol, "reason": "missing_rejection_timestamp"})
            rows.append(
                {
                    "symbol": st.symbol,
                    "timestamp": st.observed_at,
                    "price": observed_price,
                    "reason": st.final_rejection_reason,
                    "missing_reason": "missing_rejection_timestamp",
                }
            )
            continue
        if observed_price is None or observed_price <= 0:
            missing.append({"symbol": st.symbol, "reason": "missing_rejection_price"})
            rows.append(
                {
                    "symbol": st.symbol,
                    "timestamp": st.observed_at,
                    "price": observed_price,
                    "reason": st.final_rejection_reason,
                    "missing_reason": "missing_rejection_price",
                }
            )
            continue
        if st.symbol not in bars_cache:
            bars_cache[st.symbol] = _load_local_bars_for_symbol(
                data_dir=data_dir,
                bars_dir=bars_dir,
                symbol=st.symbol,
                day=day,
            )
        bars = bars_cache.get(st.symbol)
        if bars is None or bars.empty:
            missing.append({"symbol": st.symbol, "reason": "missing_local_bars"})
        row = {
            "symbol": st.symbol,
            "timestamp": observed_at.isoformat(),
            "price": observed_price,
            "reason": st.final_rejection_reason,
            "return_15m_pct": _close_forward_return_from_bars(
                bars,
                observed_at=observed_at,
                observed_price=observed_price,
                minutes=15,
            ),
            "return_30m_pct": _close_forward_return_from_bars(
                bars,
                observed_at=observed_at,
                observed_price=observed_price,
                minutes=30,
            ),
            "return_60m_pct": _close_forward_return_from_bars(
                bars,
                observed_at=observed_at,
                observed_price=observed_price,
                minutes=60,
            ),
            "return_eod_pct": _close_forward_return_from_bars(
                bars,
                observed_at=observed_at,
                observed_price=observed_price,
                minutes=None,
            ),
            "source_path": st.source_path,
        }
        if all(row.get(key) is None for key in ("return_15m_pct", "return_30m_pct", "return_60m_pct", "return_eod_pct")):
            row["missing_reason"] = "missing_forward_closes"
            if bars is not None and not bars.empty:
                missing.append({"symbol": st.symbol, "reason": "missing_forward_closes"})
        rows.append(row)
    return {
        "reject_reason_filter": _ENTRY_ALIGNMENT_BREAKOUT_TEXT,
        "total_rejects": len(rejects),
        "rows_with_any_return": sum(
            1
            for row in rows
            if any(row.get(key) is not None for key in ("return_15m_pct", "return_30m_pct", "return_60m_pct", "return_eod_pct"))
        ),
        "summary_by_horizon": {
            "15m": _return_summary(rows, "return_15m_pct"),
            "30m": _return_summary(rows, "return_30m_pct"),
            "60m": _return_summary(rows, "return_60m_pct"),
            "eod": _return_summary(rows, "return_eod_pct"),
        },
        "missing_outcomes": missing,
        "examples": rows,
    }


def latest_dynamic_gate_research_date(
    *,
    project_root: Path | str = ".",
    data_dir: Path | str = "data",
    user_id: str = "live_bot",
) -> str | None:
    root = Path(project_root)
    data = Path(data_dir)
    candidates: set[str] = set()
    for path in _history_paths(data, day="0000-00-00", user_id=user_id):
        day = _date_from_path(path)
        if day:
            candidates.add(day)
    history_root = data / "dynamic_scan_history"
    if history_root.exists():
        safe_user = _safe_user(user_id)
        for path in history_root.glob("*.json"):
            if path.name.endswith(f"_{safe_user}.json") or path.name.endswith("_default.json"):
                day = _date_from_path(path)
                if day:
                    candidates.add(day)
    for root_dir in (root / "data" / "logs", root / "reports" / "debug"):
        if root_dir.exists():
            for path in root_dir.glob("*"):
                day = _date_from_path(path)
                if day:
                    candidates.add(day)
    return max(candidates) if candidates else None


def _ingest_scan_history(states: dict[str, SymbolGateState], *, paths: Sequence[Path]) -> None:
    for path in paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        generated_at = payload.get("generated_at")
        for row in payload.get("accepted") or []:
            if not isinstance(row, Mapping):
                continue
            sym = str(row.get("symbol") or "").strip().upper()
            if not sym:
                continue
            st = _state(states, sym)
            st.seen_dynamic = True
            st.observed_at = st.observed_at or str(row.get("timestamp") or generated_at or "")
            st.observed_price = st.observed_price or _safe_float_or_none(
                row.get("price", row.get("last_price", row.get("close", row.get("current_price"))))
            )
            st.source_path = st.source_path or str(path)
            if sym in set(str(s).upper() for s in payload.get("selected") or []):
                st.selected_dynamic = True
            st.note(event="dynamic_scan_accepted", score=row.get("score"))
        for row in payload.get("rejected") or []:
            if not isinstance(row, Mapping):
                continue
            sym = str(row.get("symbol") or "").strip().upper()
            if not sym:
                continue
            st = _state(states, sym)
            st.seen_dynamic = True
            st.observed_at = str(row.get("timestamp") or generated_at or "")
            st.observed_price = _safe_float_or_none(
                row.get("price", row.get("last_price", row.get("close", row.get("current_price"))))
            )
            st.source_path = str(path)
            reason = str(row.get("rejection_reason") or "unknown")
            st.note(event="dynamic_scan_rejected", reason=reason, score=row.get("score"))


def _ingest_log_line(states: dict[str, SymbolGateState], line: str) -> None:
    kv = _parse_kv(line)
    for sym in _parse_selected_list(line):
        st = _state(states, sym)
        st.seen_dynamic = True
        st.selected_dynamic = True
        st.note(event="dynamic_scan_selected", line=line)
    if "DYNAMIC_SELECTED" in line:
        sym = _symbol_from_line(line, kv)
        if sym:
            st = _state(states, sym)
            st.seen_dynamic = True
            st.selected_dynamic = True
            st.note(event="dynamic_selected", score=kv.get("score"), line=line)
    if "DYNAMIC_SCAN reject" in line:
        sym = _symbol_from_line(line, kv)
        if sym:
            reason = _reason_from_line(line, kv)
            st = _state(states, sym)
            st.seen_dynamic = True
            st.note(event="dynamic_scan_rejected", reason=reason, line=line, score=kv.get("score"))
    if "ENTRY_EVAL" in line:
        sym = _symbol_from_line(line, kv)
        if sym:
            st = _state(states, sym)
            final = str(kv.get("final") or "").upper()
            if final in {"T", "TRUE", "1"}:
                st.entry_eval_passed = True
            reason = None if st.entry_eval_passed else _reason_from_line(line, kv)
            st.note(event="entry_eval", reason=reason, line=line)
    reject_markers = (
        "ALLOCATOR_REJECT_REASON",
        "ALLOCATOR_FILTER_REJECT",
        "ALLOCATOR_SKIP_REASON",
        "ALLOCATOR_NO_ACTION_DETAIL",
        "DYNAMIC_NOT_TRADABLE",
        "DYNAMIC_REJECT",
        "ALLOCATOR_REJECT ",
    )
    if any(marker in line for marker in reject_markers):
        sym = _symbol_from_line(line, kv)
        if sym:
            reason = _reason_from_line(line, kv)
            st = _state(states, sym)
            st.note(event="rejected", reason=reason, line=line, score=kv.get("score"))
    if "ALLOCATOR_ACTION_CREATED" in line:
        sym = _symbol_from_line(line, kv)
        if sym:
            st = _state(states, sym)
            st.allocator_action_created = True
            st.note(event="allocator_action_created", line=line)
    if "ALLOCATOR_ACTION_SUBMITTED" in line or re.search(r"\b(BUY|SELL)\b", line):
        sym = _symbol_from_line(line, kv)
        if sym:
            st = _state(states, sym)
            st.became_order = True
            st.note(event="order_submitted", line=line)

    downstream_markers = (
        "ENTRY_TERMINAL_OUTCOME",
        "ALLOCATOR_ACTION_BLOCKED",
        "ALLOCATOR_ACTION_POST_CHECK_EXIT",
        "ORDER_BUILD_REJECT",
        "TRADE_CYCLE_GATE",
    )
    if any(marker in line for marker in downstream_markers):
        sym = _symbol_from_line(line, kv)
        if sym:
            reason = _reason_from_line(line, kv)
            st = _state(states, sym)
            st.note(event="downstream_block", reason=reason, line=line, score=kv.get("score"))


def _ingest_logs(states: dict[str, SymbolGateState], *, paths: Sequence[Path]) -> list[str]:
    used: list[str] = []
    for path in paths:
        try:
            text = _read_text(path)
        except Exception:
            continue
        used.append(str(path))
        for line in text.splitlines():
            if any(
                marker in line
                for marker in (
                    "DYNAMIC_SELECTED",
                    "DYNAMIC_SCAN selected=",
                    "DYNAMIC_SCAN reject",
                    "DYNAMIC_REJECT",
                    "DYNAMIC_NOT_TRADABLE",
                    "ENTRY_EVAL",
                    "ALLOCATOR_REJECT",
                    "ALLOCATOR_FILTER_REJECT",
                    "ALLOCATOR_SKIP_REASON",
                    "ALLOCATOR_NO_ACTION_DETAIL",
                    "ALLOCATOR_ACTION_CREATED",
                    "ENTRY_TERMINAL_OUTCOME",
                    "ALLOCATOR_ACTION_BLOCKED",
                    "ALLOCATOR_ACTION_POST_CHECK_EXIT",
                    "ACTION_SUBMIT",
                    "ORDER_BUILD_REJECT",
                    "TRADE_CYCLE_GATE",
                )
            ):
                _ingest_log_line(states, line)
    return used


def _json_loads_maybe(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except Exception:
        return None


def _iter_sqlite_rows(conn: sqlite3.Connection, sql: str, args: tuple[Any, ...]) -> Iterable[sqlite3.Row]:
    try:
        return list(conn.execute(sql, args))
    except sqlite3.Error:
        return []


def _ingest_sqlite_event_store(
    states: dict[str, SymbolGateState],
    *,
    data_dir: Path,
    day: str,
    user_id: str,
) -> str | None:
    db_path = data_dir / "algo_live.db"
    if not db_path.exists():
        return None
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error:
        return None
    try:
        day_prefix = f"{day}%"
        user_args = (user_id, day_prefix)
        for row in _iter_sqlite_rows(
            conn,
            "select ts, selected_json, candidates_json, payload_json from dynamic_scans where user_id = ? and ts like ? order by ts",
            user_args,
        ):
            selected = _json_loads_maybe(row["selected_json"])
            selected_set = {str(sym).strip().upper() for sym in selected or [] if str(sym).strip()}
            for sym in selected_set:
                st = _state(states, sym)
                st.seen_dynamic = True
                st.selected_dynamic = True
                st.note(event="dynamic_scan_selected_sqlite", line=f"sqlite:{db_path}")
            candidates = _json_loads_maybe(row["candidates_json"])
            if candidates is None:
                payload = _json_loads_maybe(row["payload_json"])
                candidates = payload.get("candidates") if isinstance(payload, Mapping) else None
            for candidate in candidates or []:
                if not isinstance(candidate, Mapping):
                    continue
                sym = str(candidate.get("symbol") or "").strip().upper()
                if not sym:
                    continue
                st = _state(states, sym)
                st.seen_dynamic = True
                if sym in selected_set or bool(candidate.get("selected")):
                    st.selected_dynamic = True
                accepted = candidate.get("accepted")
                reason = str(candidate.get("rejection_reason") or candidate.get("reason") or "")
                st.note(
                    event="dynamic_scan_candidate_sqlite",
                    reason=reason if accepted is False and reason else None,
                    score=candidate.get("score"),
                    line=f"sqlite:{db_path}",
                )
        for row in _iter_sqlite_rows(
            conn,
            "select ts, symbol, route, final, reason from entry_evaluations where user_id = ? and ts like ? order by ts",
            user_args,
        ):
            sym = str(row["symbol"] or "").strip().upper()
            if not sym:
                continue
            st = _state(states, sym)
            final = bool(row["final"])
            if final:
                st.entry_eval_passed = True
            reason = None if final else str(row["reason"] or "unknown")
            st.note(
                event="entry_eval_sqlite",
                reason=reason,
                line=f"sqlite:{db_path} route={row['route']} final={final}",
            )
        for row in _iter_sqlite_rows(
            conn,
            "select ts, symbol, status, order_id from trades where user_id = ? and ts like ? order by ts",
            user_args,
        ):
            sym = str(row["symbol"] or "").strip().upper()
            if not sym:
                continue
            st = _state(states, sym)
            st.became_order = True
            st.note(
                event="order_sqlite",
                line=f"sqlite:{db_path} status={row['status']} order_id={row['order_id']}",
            )
        for row in _iter_sqlite_rows(
            conn,
            (
                "select ts, symbol, route, stage, reason, payload_json "
                "from entry_terminal_outcomes where user_id = ? and ts like ? order by ts"
            ),
            user_args,
        ):
            sym = str(row["symbol"] or "").strip().upper()
            if not sym:
                continue
            stage = str(row["stage"] or "unknown")
            reason = str(row["reason"] or stage)
            payload_json = str(row["payload_json"] or "{}")
            st = _state(states, sym)
            if stage == "allocator_action_created":
                st.allocator_action_created = True
            if stage == "submitted":
                st.became_order = True
            st.note(
                event="entry_terminal_outcome_sqlite",
                reason=reason,
                line=(
                    f"sqlite:{db_path} route={row['route']} stage={stage} "
                    f"reason={reason} payload_json={payload_json}"
                ),
            )
    finally:
        conn.close()
    return str(db_path)


_ACCOUNTED_ALLOCATOR_TERMINAL_STAGES = {
    "allocator_appended",
    "allocator_input",
    "allocator_no_action",
    "allocator_order_intent",
    "allocator_action_created",
    "submitted",
    "skipped_with_reason",
    "allocator_filtered",
    "order_builder_rejected",
    "risk_rejected",
    "broker_rejected",
}


def _has_accounted_allocator_terminal(st: SymbolGateState) -> bool:
    for event in st.events:
        line = str(event.get("line") or "")
        kv = _parse_kv(line)
        stage = str(kv.get("stage") or "").strip()
        if stage in _ACCOUNTED_ALLOCATOR_TERMINAL_STAGES:
            return True
    return bool(st.allocator_action_created or st.became_order)


def _ingest_trade_attribution_terminal_hints(
    states: dict[str, SymbolGateState],
    *,
    data_dir: Path,
    day: str,
    user_id: str,
) -> str | None:
    path = data_dir / "trade_attribution" / "daily" / f"{day}_{_safe_user(user_id)}.json"
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, Mapping):
        return None
    candidates = payload.get("candidates") if isinstance(payload.get("candidates"), list) else []
    allocator_candidates = (
        payload.get("allocator_candidates")
        if isinstance(payload.get("allocator_candidates"), list)
        else []
    )
    orders = payload.get("orders") if isinstance(payload.get("orders"), list) else []
    allocator_symbols = {
        str(row.get("symbol") or "").strip().upper()
        for row in allocator_candidates
        if isinstance(row, Mapping) and str(row.get("symbol") or "").strip()
    }
    order_symbols = {
        str(row.get("symbol") or "").strip().upper()
        for row in orders
        if isinstance(row, Mapping) and str(row.get("symbol") or "").strip()
    }
    for row in candidates:
        if not isinstance(row, Mapping):
            continue
        sym = str(row.get("symbol") or "").strip().upper()
        if not sym:
            continue
        accepted = row.get("accepted")
        if accepted is not True and str(accepted).strip().lower() not in {"1", "true", "yes"}:
            continue
        if sym in allocator_symbols or sym in order_symbols:
            continue
        st = _state(states, sym)
        if not st.entry_eval_passed:
            continue
        if _has_accounted_allocator_terminal(st):
            continue
        st.note(
            event="entry_terminal_outcome_attribution",
            reason="legacy_no_allocator_candidate_recorded",
            line=(
                f"attribution:{path} stage=allocator_input_missing "
                "reason=legacy_no_allocator_candidate_recorded"
            ),
        )
    return str(path)


def _trace_reason(trace: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = trace.get(key)
        if isinstance(value, Mapping):
            reason = value.get("reason")
            result = value.get("result")
            if reason and str(reason) not in {"accepted", "created", "ok", "none"}:
                return str(reason)
            if result is False and reason:
                return str(reason)
    return None


def _ingest_replay_market_session(
    states: dict[str, SymbolGateState],
    *,
    data_dir: Path,
    day: str,
    user_id: str,
) -> list[str]:
    used: list[str] = []
    for replay_path in _replay_market_session_paths(data_dir, day=day, user_id=user_id):
        try:
            payload = json.loads(replay_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        used.append(str(replay_path))
        for trace in payload.get("per_symbol_trace") or []:
            if not isinstance(trace, Mapping):
                continue
            sym = str(trace.get("symbol") or "").strip().upper()
            if not sym:
                continue
            route = str(trace.get("route") or "")
            source = str(trace.get("source") or "")
            st = _state(states, sym)
            if bool(trace.get("scan_selected")) or "dynamic" in route or "dynamic" in source:
                st.seen_dynamic = True
                st.selected_dynamic = True
            entry_eval = trace.get("entry_eval") if isinstance(trace.get("entry_eval"), Mapping) else {}
            if bool(entry_eval.get("result")):
                st.entry_eval_passed = True
                st.note(event="entry_eval_replay", line=f"replay:{replay_path} route={route} final=True")
            elif entry_eval:
                st.note(
                    event="entry_eval_replay",
                    reason=str(entry_eval.get("reason") or "entry_eval_reject"),
                    line=f"replay:{replay_path} route={route} final=False",
                )
            allocator_action = (
                trace.get("allocator_action")
                if isinstance(trace.get("allocator_action"), Mapping)
                else {}
            )
            if bool(allocator_action.get("result")):
                st.allocator_action_created = True
                st.note(event="allocator_action_created", line=f"replay:{replay_path} stage=allocator_action_created")
            order_build = trace.get("order_build") if isinstance(trace.get("order_build"), Mapping) else {}
            simulated_submit = (
                trace.get("simulated_submit")
                if isinstance(trace.get("simulated_submit"), Mapping)
                else {}
            )
            if bool(simulated_submit.get("result")):
                st.became_order = True
                st.note(event="order_submitted", line=f"replay:{replay_path} stage=submitted")
                continue
            reason = _trace_reason(
                trace,
                "simulated_submit",
                "order_build",
                "allocator_action",
                "allocator_candidate",
            )
            if reason:
                stage = "order_builder_rejected" if order_build else "allocator_filtered"
                if str(reason) in {"no_allocator_action", "not_reached", "not_seen_by_allocator"}:
                    stage = "missing_terminal_record"
                st.note(
                    event="downstream_block",
                    reason=reason,
                    line=f"replay:{replay_path} stage={stage} reason={reason} route={route} source={source}",
                )
        for key, stage in (
            ("rejected_by_allocator", "allocator_filtered"),
            ("rejected_by_order_builder", "order_builder_rejected"),
            ("order_build_rejects", "order_builder_rejected"),
        ):
            for row in payload.get(key) or []:
                if not isinstance(row, Mapping):
                    continue
                sym = str(row.get("symbol") or "").strip().upper()
                if not sym:
                    continue
                st = _state(states, sym)
                line = str(row.get("line") or f"replay:{replay_path} stage={stage}")
                reason = str(row.get("reason") or _reason_from_line(line, _parse_kv(line)))
                st.note(event="downstream_block", reason=reason, line=f"{line} stage={stage}")
    return used


def _finalize_missing_outcomes(states: Mapping[str, SymbolGateState]) -> None:
    for st in states.values():
        if st.final_gate and st.final_gate != "unknown":
            continue
        if not st.seen_dynamic and not st.selected_dynamic:
            st.final_gate = "core_symbol_non_dynamic"
            st.final_rejection_reason = st.final_rejection_reason or "core_symbol_non_dynamic"
            continue
        if st.selected_dynamic and not st.became_order:
            st.final_gate = "missing_outcome"
            st.final_rejection_reason = st.final_rejection_reason or "missing_outcome"
            continue
        if st.seen_dynamic and not st.final_gate:
            st.final_gate = "missing_outcome"
            st.final_rejection_reason = st.final_rejection_reason or "missing_outcome"


def _line_config_values(line: str | None) -> dict[str, str]:
    if not line:
        return {}
    kv = _parse_kv(line)
    config_keys = {
        "trade_cycle_allowed",
        "minimum_cash_to_deploy",
        "min_order_notional",
        "min_realloc_leg",
        "available_cash",
        "cash_reserve",
        "gross_headroom",
        "current_dynamic_sleeve_usage",
        "dynamic_sleeve_cap",
        "max_single_dynamic_notional",
        "max_positions",
        "cooldown_active",
        "last_removal_stage",
        "limiting_cap",
        "candidate_notional_requested",
        "candidate_notional",
        "target_allocation",
        "trade_size",
        "final_trade_size",
    }
    out = {key: value for key, value in kv.items() if key in config_keys}
    for key in ("minimum_cash_to_deploy",):
        if key in out:
            continue
        match = re.search(rf"\b{key}\s+([0-9]+(?:\.[0-9]+)?)\b", line)
        if match:
            out[key] = match.group(1)
    payload_match = re.search(r"payload_json=(\{.*\})$", line)
    if payload_match:
        try:
            payload = json.loads(payload_match.group(1))
        except (TypeError, ValueError):
            payload = {}
        if isinstance(payload, Mapping):
            for key in config_keys:
                if key in out or key not in payload:
                    continue
                value = payload.get(key)
                if value is not None:
                    out[key] = str(value)
    return out


def _entry_alignment_diagnostics(reason: str | None) -> dict[str, Any] | None:
    text = str(reason or "")
    if "entry_alignment" not in text:
        return None
    bools = {
        "breakout": None,
        "new_high": None,
        "green_1m": None,
        "opening_range_breakout": None,
    }
    match = re.search(
        r"breakout=(True|False).*?nh=(True|False).*?green=(True|False).*?orb=(True|False)",
        text,
        flags=re.IGNORECASE,
    )
    if match:
        bools = {
            "breakout": match.group(1).lower() == "true",
            "new_high": match.group(2).lower() == "true",
            "green_1m": match.group(3).lower() == "true",
            "opening_range_breakout": match.group(4).lower() == "true",
        }
    passed = [key for key, value in bools.items() if value is True]
    return {
        **bools,
        "closest_to_passing": passed or ["none_logged_true"],
        "catalyst_override_would_have_helped": "not_evaluated_diagnostics_only",
    }


def _latest_downstream_event(st: SymbolGateState) -> dict[str, Any] | None:
    for event in reversed(st.events):
        line = str(event.get("line") or "")
        name = str(event.get("event") or "")
        if (
            name == "downstream_block"
            or name == "entry_terminal_outcome_sqlite"
            or name == "entry_terminal_outcome_attribution"
            or "ALLOCATOR_" in line
            or "ENTRY_TERMINAL_OUTCOME" in line
            or "attribution:" in line
            or "ORDER_BUILD_REJECT" in line
            or "TRADE_CYCLE_GATE" in line
        ):
            return event
    return None


def _entry_passed_downstream_row(st: SymbolGateState) -> dict[str, Any]:
    event = _latest_downstream_event(st)
    if event is not None:
        reason = str(event.get("reason") or st.final_rejection_reason or "allocator_or_execution_reject")
        line = str(event.get("line") or "")
        kv = _parse_kv(line)
        gate = str(kv.get("stage") or normalize_gate_reason(reason, line))
    elif st.allocator_action_created and not st.became_order:
        reason = "allocator_action_created_but_no_submit_logged"
        gate = "missing_submit_outcome"
        line = ""
    elif st.final_gate and st.final_gate not in {"entry_alignment", "missing_outcome", "core_symbol_non_dynamic"}:
        reason = str(st.final_rejection_reason or st.final_gate)
        gate = str(st.final_gate)
        line = ""
    else:
        reason = "legacy_missing_terminal_outcome_no_event_store_record"
        gate = "legacy_missing_terminal_outcome"
        line = ""
    return {
        "symbol": st.symbol,
        "dynamic_score": st.dynamic_score,
        "selected_dynamic": st.selected_dynamic,
        "seen_dynamic": st.seen_dynamic,
        "dynamic_candidate": bool(st.seen_dynamic or st.selected_dynamic),
        "allocator_action_created": st.allocator_action_created,
        "order_submitted": st.became_order,
        "downstream_block_stage": gate,
        "downstream_block_reason": reason,
        "downstream_category": downstream_gate_category(reason, line, gate),
        "config_values": _line_config_values(line),
        "entry_alignment_diagnostics": _entry_alignment_diagnostics(st.final_rejection_reason),
    }


def _candidate_downstream_block(st: SymbolGateState) -> tuple[str | None, str | None, dict[str, str]]:
    if st.became_order:
        return None, None, {}
    if st.entry_eval_passed:
        row = _entry_passed_downstream_row(st)
        return (
            str(row.get("downstream_block_reason") or ""),
            str(row.get("downstream_block_stage") or ""),
            dict(row.get("config_values") or {}),
        )
    return st.final_rejection_reason, st.final_gate, {}


def _candidate_report_row(st: SymbolGateState) -> dict[str, Any]:
    block_reason, block_stage, block_config = _candidate_downstream_block(st)
    block_category = (
        downstream_gate_category(block_reason, "", block_stage)
        if block_reason or block_stage
        else None
    )
    return {
        "symbol": st.symbol,
        "dynamic_score": st.dynamic_score,
        "seen_dynamic": st.seen_dynamic,
        "selected_dynamic": st.selected_dynamic,
        "selected": st.selected_dynamic,
        "entry_eval_passed": st.entry_eval_passed,
        "final_signal": st.entry_eval_passed,
        "final": st.entry_eval_passed,
        "allocator_action_created": st.allocator_action_created,
        "became_order": st.became_order,
        "order_submitted": st.became_order,
        "final_gate": st.final_gate,
        "final_rejection_reason": st.final_rejection_reason,
        "downstream_block_reason": block_reason,
        "downstream_block_stage": block_stage,
        "downstream_category": block_category,
        "downstream_block_config_values": block_config,
        "entry_alignment_diagnostics": _entry_alignment_diagnostics(st.final_rejection_reason),
    }


def build_dynamic_gate_research_report(
    *,
    project_root: Path | str = ".",
    data_dir: Path | str = "data",
    day: str,
    user_id: str = "live_bot",
    log_paths: Sequence[Path | str] | None = None,
    bars_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Build a read-only dynamic momentum gate report from local files."""
    root = Path(project_root)
    data = Path(data_dir)
    states: dict[str, SymbolGateState] = {}
    history = sorted(
        dict.fromkeys(
            [
                *_history_paths(data, day=day, user_id=user_id),
                *_replay_history_paths(data, day=day, user_id=user_id),
            ]
        )
    )
    _ingest_scan_history(states, paths=history)
    sqlite_path = _ingest_sqlite_event_store(states, data_dir=data, day=day, user_id=user_id)
    discovered_logs = discover_dynamic_gate_log_paths(project_root=root, day=day, extra_paths=log_paths)
    used_logs = _ingest_logs(states, paths=discovered_logs)
    replay_paths = _ingest_replay_market_session(states, data_dir=data, day=day, user_id=user_id)
    attribution_path = _ingest_trade_attribution_terminal_hints(
        states,
        data_dir=data,
        day=day,
        user_id=user_id,
    )
    _finalize_missing_outcomes(states)
    rows = sorted(states.values(), key=lambda st: (-st.dynamic_score, st.symbol))
    selected = [st for st in rows if st.selected_dynamic]
    dynamic_rows = [st for st in rows if st.seen_dynamic or st.selected_dynamic]
    entry_passed = [st for st in rows if st.entry_eval_passed]
    dynamic_entry_passed = [
        st for st in dynamic_rows if st.entry_eval_passed
    ]
    rejected = [st for st in rows if st.final_gate]
    counts = Counter(st.final_gate or "unknown" for st in rejected)
    examples: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for st in rejected:
        if len(examples[st.final_gate or "unknown"]) >= 5:
            continue
        examples[st.final_gate or "unknown"].append(
            {
                "symbol": st.symbol,
                "reason": st.final_rejection_reason,
                "dynamic_score": st.dynamic_score,
            }
        )
    scanner_no_order = [st for st in selected if not st.became_order]
    entry_allocator_rejects = [
        st for st in entry_passed if st.final_gate and not st.became_order
    ]
    dynamic_entry_allocator_rejects = [
        st for st in dynamic_entry_passed if not st.became_order
    ]
    entry_downstream_rejections = [
        _entry_passed_downstream_row(st)
        for st in entry_passed
        if not st.became_order
    ]
    dynamic_entry_downstream_rejections = [
        row for row in entry_downstream_rejections if bool(row.get("dynamic_candidate"))
    ]
    downstream_counts = Counter(
        str(row.get("downstream_category") or "unknown")
        for row in entry_downstream_rejections
    )
    entry_alignment_forward_returns = _entry_alignment_forward_return_research(
        states,
        data_dir=data,
        day=day,
        bars_dir=bars_dir,
    )
    return {
        "date": day,
        "user_id": user_id,
        "source_files": {
            "dynamic_scan_history": [str(path) for path in history],
            "sqlite_event_store": sqlite_path,
            "trade_attribution": attribution_path,
            "logs": used_logs,
            "replay_market_session": replay_paths,
        },
        "summary": {
            "dynamic_candidates_seen": len(rows),
            "dynamic_scope_candidates_seen": len(dynamic_rows),
            "selected_dynamic_candidates": len(selected),
            "entry_eval_passed": len(entry_passed),
            "dynamic_entry_eval_passed": len(dynamic_entry_passed),
            "scanner_passed_never_orders": len(scanner_no_order),
            "entry_eval_passed_allocator_rejected": len(entry_allocator_rejects),
            "dynamic_entry_eval_passed_allocator_rejected": len(dynamic_entry_allocator_rejects),
        },
        "rejection_counts_by_gate": dict(sorted(counts.items())),
        "downstream_rejection_counts_by_category": dict(sorted(downstream_counts.items())),
        "unclassified_examples": [
            {
                "symbol": st.symbol,
                "reason": st.final_rejection_reason,
                "dynamic_score": st.dynamic_score,
            }
            for st in rejected
            if st.final_gate == "unknown"
        ][:20],
        "gate_buckets": {
            "no_catalyst_rejects": [st.symbol for st in rejected if st.final_gate == "no_catalyst"],
            "short_history_rejects": [st.symbol for st in rejected if st.final_gate == "short_history"],
            "spread_cap_rejects": [st.symbol for st in rejected if st.final_gate == "spread_cap"],
            "entry_alignment_rejects": [st.symbol for st in rejected if st.final_gate == "entry_alignment"],
            "vwap_extension_rejects": [st.symbol for st in rejected if st.final_gate == "vwap_extension"],
            "relative_volume_rejects": [st.symbol for st in rejected if st.final_gate == "relative_volume"],
            "unstable_quote_rejects": [st.symbol for st in rejected if st.final_gate == "unstable_quote"],
            "below_min_price_rejects": [st.symbol for st in rejected if st.final_gate == "below_min_price"],
            "excessive_gain_rejects": [st.symbol for st in rejected if st.final_gate == "excessive_gain"],
            "no_decision_rejects": [st.symbol for st in rejected if st.final_gate == "no_decision"],
            "bad_quote_rejects": [st.symbol for st in rejected if st.final_gate == "bad_quote"],
            "allocator_rejects": [st.symbol for st in rejected if st.final_gate == "allocator_reject"],
            "entry_eval_rejects": [st.symbol for st in rejected if st.final_gate == "entry_eval_reject"],
            "prefilter_rejects": [st.symbol for st in rejected if st.final_gate == "prefilter_reject"],
            "core_symbol_non_dynamic": [st.symbol for st in rejected if st.final_gate == "core_symbol_non_dynamic"],
            "missing_outcome": [st.symbol for st in rejected if st.final_gate == "missing_outcome"],
        },
        "symbols_passed_scanner_never_orders": [st.symbol for st in scanner_no_order],
        "symbols_passed_entry_eval_rejected_by_allocator": [st.symbol for st in entry_allocator_rejects],
        "entry_eval_passed_downstream_rejections": entry_downstream_rejections,
        "dynamic_entry_eval_passed_downstream_rejections": dynamic_entry_downstream_rejections,
        "entry_alignment_forward_returns": entry_alignment_forward_returns,
        "top_missed_candidates_by_dynamic_score": [
            {
                "symbol": st.symbol,
                "dynamic_score": st.dynamic_score,
                "final_gate": st.final_gate,
                "final_rejection_reason": st.final_rejection_reason,
            }
            for st in scanner_no_order[:20]
        ],
        "examples_per_rejection_reason": dict(examples),
        "candidates": [_candidate_report_row(st) for st in rows],
    }


def render_dynamic_gate_research_report(report: Mapping[str, Any]) -> str:
    lines = [
        f"Dynamic Momentum Gate Research - {report.get('date')} user={report.get('user_id')}",
        f"June Session Diagnostic Report - {report.get('date')} user={report.get('user_id')}",
        "Research-only. No trading behavior changed.",
        "",
    ]
    summary = report.get("summary") if isinstance(report.get("summary"), Mapping) else {}
    for key in (
        "dynamic_candidates_seen",
        "dynamic_scope_candidates_seen",
        "selected_dynamic_candidates",
        "entry_eval_passed",
        "dynamic_entry_eval_passed",
        "scanner_passed_never_orders",
        "entry_eval_passed_allocator_rejected",
        "dynamic_entry_eval_passed_allocator_rejected",
    ):
        lines.append(f"{key}: {summary.get(key, 0)}")
    output_files = report.get("output_files") if isinstance(report.get("output_files"), Mapping) else {}
    if output_files:
        lines.append(f"json_output: {output_files.get('json')}")
        lines.append(f"text_output: {output_files.get('text')}")
    lines.append("")
    lines.append("Rejection counts by gate:")
    counts = report.get("rejection_counts_by_gate") if isinstance(report.get("rejection_counts_by_gate"), Mapping) else {}
    if counts:
        for gate, count in sorted(counts.items(), key=lambda item: (-int(item[1]), str(item[0]))):
            lines.append(f"- {gate}: {count}")
    else:
        lines.append("- none")
    lines.append("")
    downstream_counts = (
        report.get("downstream_rejection_counts_by_category")
        if isinstance(report.get("downstream_rejection_counts_by_category"), Mapping)
        else {}
    )
    lines.append("Downstream rejection attribution:")
    if downstream_counts:
        for category, count in sorted(downstream_counts.items(), key=lambda item: (-int(item[1]), str(item[0]))):
            lines.append(f"- {category}: {count}")
    else:
        lines.append("- none")
    lines.append("")
    lines.append("Top missed candidates by dynamic score:")
    for row in list(report.get("top_missed_candidates_by_dynamic_score") or [])[:10]:
        lines.append(
            "- {symbol}: score={score:.2f} gate={gate} reason={reason}".format(
                symbol=row.get("symbol"),
                score=_safe_float(row.get("dynamic_score")),
                gate=row.get("final_gate") or "none",
                reason=row.get("final_rejection_reason") or "none",
            )
        )
    if not report.get("top_missed_candidates_by_dynamic_score"):
        lines.append("- none")
    lines.append("")
    lines.append("Entry-eval passed downstream rejections:")
    entry_rows = list(report.get("entry_eval_passed_downstream_rejections") or [])
    if entry_rows:
        for row in entry_rows:
            cfg = row.get("config_values") if isinstance(row.get("config_values"), Mapping) else {}
            cfg_text = (
                " config="
                + ",".join(f"{key}={value}" for key, value in sorted(cfg.items()))
                if cfg
                else ""
            )
            lines.append(
                "- {symbol}: dynamic={dynamic} category={category} stage={stage} reason={reason}{cfg}".format(
                    symbol=row.get("symbol"),
                    dynamic=bool(row.get("dynamic_candidate")),
                    category=row.get("downstream_category") or "unknown",
                    stage=row.get("downstream_block_stage") or "unknown",
                    reason=row.get("downstream_block_reason") or "unknown",
                    cfg=cfg_text,
                )
            )
    else:
        lines.append("- none")
    lines.append("")
    lines.append("Entry-alignment forward returns:")
    align_returns = (
        report.get("entry_alignment_forward_returns")
        if isinstance(report.get("entry_alignment_forward_returns"), Mapping)
        else {}
    )
    lines.append(
        "reject_filter: {reason}".format(
            reason=align_returns.get("reject_reason_filter") or _ENTRY_ALIGNMENT_BREAKOUT_TEXT,
        )
    )
    lines.append(
        "total_rejects: {total} rows_with_any_return: {with_returns}".format(
            total=int(align_returns.get("total_rejects") or 0),
            with_returns=int(align_returns.get("rows_with_any_return") or 0),
        )
    )
    summary_by_horizon = (
        align_returns.get("summary_by_horizon")
        if isinstance(align_returns.get("summary_by_horizon"), Mapping)
        else {}
    )
    if summary_by_horizon:
        for label in ("15m", "30m", "60m", "eod"):
            stats = summary_by_horizon.get(label) if isinstance(summary_by_horizon.get(label), Mapping) else {}
            count = int(stats.get("count") or 0)
            avg = stats.get("average_return_pct")
            med = stats.get("median_return_pct")
            win_rate = stats.get("win_rate")
            avg_text = "n/a" if avg is None else f"{float(avg):.2f}%"
            med_text = "n/a" if med is None else f"{float(med):.2f}%"
            win_text = "n/a" if win_rate is None else f"{float(win_rate) * 100.0:.1f}%"
            lines.append(
                f"- {label}: count={count} avg={avg_text} median={med_text} win_rate={win_text}"
            )
            best = list(stats.get("best_examples") or [])[:3]
            worst = list(stats.get("worst_examples") or [])[:3]
            if best:
                lines.append(
                    "  best: "
                    + ", ".join(
                        "{symbol}={ret:.2f}%".format(
                            symbol=row.get("symbol"),
                            ret=_safe_float(row.get(f"return_{label}_pct" if label != "eod" else "return_eod_pct")),
                        )
                        for row in best
                    )
                )
            if worst:
                lines.append(
                    "  worst: "
                    + ", ".join(
                        "{symbol}={ret:.2f}%".format(
                            symbol=row.get("symbol"),
                            ret=_safe_float(row.get(f"return_{label}_pct" if label != "eod" else "return_eod_pct")),
                        )
                        for row in worst
                    )
                )
    else:
        lines.append("- none")
    missing = list(align_returns.get("missing_outcomes") or [])
    if missing:
        lines.append(
            "missing_outcomes: "
            + ", ".join(
                "{symbol}:{reason}".format(
                    symbol=row.get("symbol"),
                    reason=row.get("reason"),
                )
                for row in missing[:10]
            )
        )
    examples = list(align_returns.get("examples") or [])
    if examples:
        lines.append("per_symbol_returns:")
        lines.append("| Symbol | 15m | 30m | 60m | EOD | Reject Price |")
        lines.append("|---|---:|---:|---:|---:|---:|")
        for row in examples[:30]:
            def _ret_text(key: str) -> str:
                value = row.get(key)
                return "n/a" if value is None else f"{float(value):.2f}%"

            lines.append(
                "| {symbol} | {r15} | {r30} | {r60} | {eod} | {price:.2f} |".format(
                    symbol=row.get("symbol") or "",
                    r15=_ret_text("return_15m_pct"),
                    r30=_ret_text("return_30m_pct"),
                    r60=_ret_text("return_60m_pct"),
                    eod=_ret_text("return_eod_pct"),
                    price=_safe_float(row.get("price")),
                )
            )
    lines.append("")
    lines.append("Entry-alignment diagnostics:")
    align_rows = [
        row for row in list(report.get("candidates") or [])
        if isinstance(row, Mapping) and row.get("entry_alignment_diagnostics")
    ]
    if align_rows:
        for row in align_rows[:10]:
            diag = row.get("entry_alignment_diagnostics")
            diag_map = diag if isinstance(diag, Mapping) else {}
            lines.append(
                "- {symbol}: closest={closest} breakout={breakout} new_high={new_high} "
                "green_1m={green} orb={orb} catalyst_override={override}".format(
                    symbol=row.get("symbol"),
                    closest=",".join(str(x) for x in diag_map.get("closest_to_passing", [])),
                    breakout=diag_map.get("breakout"),
                    new_high=diag_map.get("new_high"),
                    green=diag_map.get("green_1m"),
                    orb=diag_map.get("opening_range_breakout"),
                    override=diag_map.get("catalyst_override_would_have_helped"),
                )
            )
    else:
        lines.append("- none")
    lines.append("")
    lines.append("Examples per rejection reason:")
    examples = report.get("examples_per_rejection_reason") if isinstance(report.get("examples_per_rejection_reason"), Mapping) else {}
    if examples:
        for gate, rows in sorted(examples.items()):
            syms = ", ".join(str(row.get("symbol")) for row in rows)
            lines.append(f"- {gate}: {syms}")
    else:
        lines.append("- none")
    unclassified = list(report.get("unclassified_examples") or [])
    if unclassified:
        lines.append("")
        lines.append("Unclassified examples:")
        for row in unclassified[:10]:
            lines.append(
                "- {symbol}: reason={reason}".format(
                    symbol=row.get("symbol"),
                    reason=row.get("reason") or "unknown",
                )
            )
    return "\n".join(lines).rstrip() + "\n"


def write_dynamic_gate_research_report(
    *,
    project_root: Path | str = ".",
    data_dir: Path | str = "data",
    day: str,
    user_id: str = "live_bot",
    log_paths: Sequence[Path | str] | None = None,
    bars_dir: Path | str | None = None,
) -> tuple[Path, Path, dict[str, Any]]:
    report = build_dynamic_gate_research_report(
        project_root=project_root,
        data_dir=data_dir,
        day=day,
        user_id=user_id,
        log_paths=log_paths,
        bars_dir=bars_dir,
    )
    out_dir = Path(data_dir) / "research" / "dynamic_gate_research"
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_user = _safe_user(user_id)
    json_path = out_dir / f"{day}_{safe_user}.json"
    txt_path = out_dir / f"{day}_{safe_user}.txt"
    report["output_files"] = {"json": str(json_path), "text": str(txt_path)}
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    txt_path.write_text(render_dynamic_gate_research_report(report), encoding="utf-8")
    return json_path, txt_path, report
