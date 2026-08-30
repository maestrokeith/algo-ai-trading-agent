from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

_ET = ZoneInfo("America/New_York")
_UTC = ZoneInfo("UTC")


@dataclass(frozen=True)
class DynamicRejectionResearchRow:
    """Research-only dynamic scanner rejection outcome row."""

    symbol: str
    timestamp: str
    price: float
    gain_pct: float
    rel_volume: float
    spread_pct: float
    news_score: int
    catalyst_score: float
    rejection_reason: str
    later_same_day_high: float | None
    later_same_day_return_pct: float | None
    source_path: str
    return_15m_pct: float | None = None
    return_30m_pct: float | None = None
    return_60m_pct: float | None = None
    return_eod_pct: float | None = None
    missing_outcome_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "timestamp": self.timestamp,
            "price": self.price,
            "gain_pct": self.gain_pct,
            "rel_volume": self.rel_volume,
            "spread_pct": self.spread_pct,
            "news_score": self.news_score,
            "catalyst_score": self.catalyst_score,
            "rejection_reason": self.rejection_reason,
            "later_same_day_high": self.later_same_day_high,
            "later_same_day_return_pct": self.later_same_day_return_pct,
            "source_path": self.source_path,
            "return_15m_pct": self.return_15m_pct,
            "return_30m_pct": self.return_30m_pct,
            "return_60m_pct": self.return_60m_pct,
            "return_eod_pct": self.return_eod_pct,
            "missing_outcome_reason": self.missing_outcome_reason,
        }


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _parse_timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _row_day(value: Any) -> str | None:
    ts = _parse_timestamp(value)
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=_ET)
    return ts.astimezone(_ET).date().isoformat()


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


def _later_same_day_high_return_from_bars(
    bars: pd.DataFrame | None,
    *,
    observed_at: datetime,
    observed_price: float,
) -> tuple[float | None, float | None]:
    if bars is None or bars.empty or observed_price <= 0:
        return None, None
    high_col = next((col for col in ("high", "High", "h") if col in bars.columns), None)
    if high_col is None:
        return None, None
    timestamps = _bar_timestamps_utc(bars)
    if timestamps is None:
        return None, None
    observed_utc = observed_at.astimezone(ZoneInfo("UTC"))
    observed_day = observed_utc.astimezone(_ET).date()
    day_mask = timestamps.dt.tz_convert(_ET).dt.date == observed_day
    later_mask = timestamps > observed_utc
    later = bars.loc[day_mask & later_mask]
    if later.empty:
        return None, None
    highs = pd.to_numeric(later[high_col], errors="coerce").dropna()
    if highs.empty:
        return None, None
    high = float(highs.max())
    return high, ((high / float(observed_price)) - 1.0) * 100.0


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
        eligible = day_bars.loc[day_ts >= observed_utc + pd.Timedelta(minutes=int(minutes))]
    if eligible.empty:
        return None
    values = pd.to_numeric(eligible[close_col], errors="coerce").dropna()
    if values.empty:
        return None
    close = float(values.iloc[-1] if minutes is None else values.iloc[0])
    return ((close / float(observed_price)) - 1.0) * 100.0


def _forward_outcomes_from_bars(
    bars: pd.DataFrame | None,
    *,
    observed_at: datetime | None,
    observed_price: float,
) -> tuple[dict[str, float | None], str | None]:
    empty: dict[str, float | None] = {
        "return_15m_pct": None,
        "return_30m_pct": None,
        "return_60m_pct": None,
        "return_eod_pct": None,
    }
    if observed_at is None:
        return empty, "missing_rejection_timestamp"
    if observed_price <= 0:
        return empty, "missing_rejection_price"
    if bars is None or bars.empty:
        return empty, "missing_local_bars"
    outcomes = {
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
    }
    if any(value is not None for value in outcomes.values()):
        return outcomes, None
    return outcomes, "missing_forward_closes"


def _artifact_paths(history_dir: Path) -> Iterable[Path]:
    if not history_dir.exists():
        return []
    return sorted(path for path in history_dir.glob("*.json") if path.is_file())


def _safe_user(user_id: str) -> str:
    return "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in str(user_id or "default"))


def _replay_market_session_paths(data_dir: Path, *, day: str, user_id: str) -> list[Path]:
    safe_user = _safe_user(user_id)
    candidates = [
        data_dir / "replay_market_session" / f"{day}_{safe_user}.json",
        data_dir / "replay_market_session" / "_cycles" / f"{day}_{safe_user}.json",
    ]
    return [path for path in candidates if path.exists()]


def _resolve_artifact_path(data_dir: Path, raw_path: Any) -> Path | None:
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
            resolved = _resolve_artifact_path(data_dir, cycle.get("history_path"))
            if resolved is not None and resolved.is_file():
                paths.append(resolved)
        resolved = _resolve_artifact_path(data_dir, payload.get("history_path"))
        if resolved is not None and resolved.is_file():
            paths.append(resolved)
    return sorted(dict.fromkeys(paths))


def latest_dynamic_rejection_date(
    *,
    data_dir: Path | str = "data",
    user_id: str = "live_bot",
    history_dir: Path | str | None = None,
) -> str | None:
    """Return the newest local dynamic rejection artifact date for a user."""
    root = Path(history_dir) if history_dir is not None else Path(data_dir) / "dynamic_scan_history"
    dates: set[str] = set()
    for path in _artifact_paths(root):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if str(payload.get("user_id") or "default") != str(user_id):
            continue
        day = _row_day(payload.get("generated_at"))
        if day:
            dates.add(day)
    replay_root = Path(data_dir) / "replay"
    safe_user = _safe_user(user_id)
    if replay_root.exists():
        for path in replay_root.glob(f"*_{safe_user}.json"):
            day = _row_day(path.name.split("_", 1)[0])
            if day:
                dates.add(day)
    replay_session_root = Path(data_dir) / "replay_market_session"
    if replay_session_root.exists():
        for path in replay_session_root.glob(f"*_{safe_user}.json"):
            day = _row_day(path.name.split("_", 1)[0])
            if day:
                dates.add(day)
    return sorted(dates)[-1] if dates else None


def _row_timestamp(raw: Mapping[str, Any], fallback: Any) -> str:
    for key in ("timestamp", "scan_timestamp", "observed_at", "quote_timestamp"):
        value = raw.get(key)
        if value:
            return str(value)
    return str(fallback or "")


def _row_price(raw: Mapping[str, Any]) -> float:
    return _safe_float(
        raw.get(
            "price",
            raw.get(
                "last_price",
                raw.get("close", raw.get("current_price", raw.get("observed_price"))),
            ),
        )
    )


def _research_row_from_raw(
    raw: Mapping[str, Any],
    *,
    timestamp_fallback: Any,
    source_path: Path,
    data_dir: Path,
    bars_dir: Path | str | None,
    bars_cache: dict[str, pd.DataFrame | None],
) -> DynamicRejectionResearchRow:
    timestamp = _row_timestamp(raw, timestamp_fallback)
    timestamp_dt = _parse_timestamp(timestamp)
    if timestamp_dt is not None and timestamp_dt.tzinfo is None:
        timestamp_dt = timestamp_dt.replace(tzinfo=_ET)
    symbol = str(raw.get("symbol") or "").strip().upper()
    price = _row_price(raw)
    later_high = (
        _safe_float(raw.get("later_same_day_high"))
        if raw.get("later_same_day_high") is not None
        else None
    )
    later_return = (
        _safe_float(raw.get("later_same_day_return_pct"))
        if raw.get("later_same_day_return_pct") is not None
        else None
    )
    bars = None
    if symbol and (later_high is None or later_return is None):
        if symbol not in bars_cache:
            bars_cache[symbol] = _load_local_bars_for_symbol(
                data_dir=data_dir,
                bars_dir=bars_dir,
                symbol=symbol,
                day=_row_day(timestamp) or "",
            )
        bars = bars_cache.get(symbol)
    if later_high is None and later_return is None and timestamp_dt is not None and price > 0:
        later_high, later_return = _later_same_day_high_return_from_bars(
            bars,
            observed_at=timestamp_dt,
            observed_price=price,
        )
    forward, missing_reason = _forward_outcomes_from_bars(
        bars,
        observed_at=timestamp_dt,
        observed_price=price,
    )
    if later_return is not None:
        missing_reason = None
    return DynamicRejectionResearchRow(
        symbol=symbol,
        timestamp=timestamp,
        price=price,
        gain_pct=_safe_float(raw.get("gain_pct", raw.get("day_gain_pct"))),
        rel_volume=_safe_float(raw.get("rel_volume", raw.get("relative_volume"))),
        spread_pct=_safe_float(raw.get("spread_pct")),
        news_score=_safe_int(raw.get("news_score")),
        catalyst_score=_safe_float(raw.get("catalyst_score")),
        rejection_reason=str(raw.get("rejection_reason") or raw.get("reason") or "unknown"),
        later_same_day_high=later_high,
        later_same_day_return_pct=later_return,
        source_path=str(source_path),
        missing_outcome_reason=missing_reason,
        **forward,
    )


def _load_replay_rejection_rows(
    *,
    data_dir: Path,
    user_id: str,
    day: str,
    bars_dir: Path | str | None,
) -> list[DynamicRejectionResearchRow]:
    safe_user = _safe_user(user_id)
    replay_paths = [
        data_dir / "replay" / f"{day}_{safe_user}.json",
        *(_replay_market_session_paths(data_dir, day=day, user_id=user_id)),
    ]
    bars_cache: dict[str, pd.DataFrame | None] = {}
    rows: list[DynamicRejectionResearchRow] = []
    fallback_timestamp = f"{day}T09:30:00-04:00"
    for replay_path in replay_paths:
        if not replay_path.exists():
            continue
        try:
            payload = json.loads(replay_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        rejected = payload.get("rejected_candidates") or payload.get("rejected_before_allocator") or []
        if not isinstance(rejected, list):
            continue
        for raw in rejected:
            if not isinstance(raw, Mapping):
                continue
            rows.append(
                _research_row_from_raw(
                    raw,
                    timestamp_fallback=fallback_timestamp,
                    source_path=replay_path,
                    data_dir=data_dir,
                    bars_dir=bars_dir,
                    bars_cache=bars_cache,
                )
            )
    return rows


def load_dynamic_rejection_rows(
    *,
    data_dir: Path | str = "data",
    user_id: str = "live_bot",
    day: str,
    history_dir: Path | str | None = None,
    bars_dir: Path | str | None = None,
) -> list[DynamicRejectionResearchRow]:
    """Load rejected dynamic candidates with later same-day outcomes from local artifacts."""
    data_root = Path(data_dir)
    root = Path(history_dir) if history_dir is not None else data_root / "dynamic_scan_history"
    bars_cache: dict[str, pd.DataFrame | None] = {}
    rows: list[DynamicRejectionResearchRow] = []
    paths = sorted(dict.fromkeys([*_artifact_paths(root), *_replay_history_paths(data_root, day=day, user_id=user_id)]))
    for path in paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if str(payload.get("user_id") or "default") != str(user_id):
            continue
        generated_day = _row_day(payload.get("generated_at"))
        if generated_day != day:
            continue
        for raw in payload.get("rejected") or []:
            if not isinstance(raw, Mapping):
                continue
            rows.append(
                _research_row_from_raw(
                    raw,
                    timestamp_fallback=payload.get("generated_at"),
                    source_path=path,
                    data_dir=data_root,
                    bars_dir=bars_dir,
                    bars_cache=bars_cache,
                )
            )
    if not rows:
        rows = _load_replay_rejection_rows(
            data_dir=data_root,
            user_id=user_id,
            day=day,
            bars_dir=bars_dir,
        )
    return rows


def build_dynamic_rejection_report(
    *,
    data_dir: Path | str = "data",
    user_id: str = "live_bot",
    day: str,
    history_dir: Path | str | None = None,
    bars_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Build a read-only report of rejected dynamic candidates that later moved."""
    rows = load_dynamic_rejection_rows(
        data_dir=data_dir,
        user_id=user_id,
        day=day,
        history_dir=history_dir,
        bars_dir=bars_dir,
    )
    rows_with_returns = [row for row in rows if row.later_same_day_return_pct is not None]
    rows_with_forward_returns = [
        row
        for row in rows
        if any(
            value is not None
            for value in (
                row.return_15m_pct,
                row.return_30m_pct,
                row.return_60m_pct,
                row.return_eod_pct,
            )
        )
    ]
    buckets = {
        "+5%": [row for row in rows_with_returns if float(row.later_same_day_return_pct or 0.0) >= 5.0],
        "+10%": [row for row in rows_with_returns if float(row.later_same_day_return_pct or 0.0) >= 10.0],
        "+20%": [row for row in rows_with_returns if float(row.later_same_day_return_pct or 0.0) >= 20.0],
    }
    sorted_buckets = {
        key: sorted(value, key=lambda row: float(row.later_same_day_return_pct or 0.0), reverse=True)
        for key, value in buckets.items()
    }
    return {
        "date": day,
        "user_id": user_id,
        "total_rejected": len(rows),
        "with_later_outcomes": len(rows_with_returns),
        "with_forward_outcomes": len(rows_with_forward_returns),
        "missing_outcome_reasons": dict(
            sorted(
                {
                    reason: sum(1 for row in rows if row.missing_outcome_reason == reason)
                    for reason in {row.missing_outcome_reason for row in rows if row.missing_outcome_reason}
                }.items()
            )
        ),
        "buckets": {key: [row.to_dict() for row in value] for key, value in sorted_buckets.items()},
        "top_missed": [
            row.to_dict()
            for row in sorted(
                rows_with_returns,
                key=lambda row: float(row.later_same_day_return_pct or 0.0),
                reverse=True,
            )[:20]
        ],
    }


def render_dynamic_rejection_report(report: Mapping[str, Any]) -> str:
    """Render a concise markdown report for missed dynamic scanner movers."""
    lines = [
        f"# Dynamic Rejection Outcome Report - {report.get('date')}",
        "",
        "Research-only report. This does not modify trading behavior.",
        "",
        f"- User: `{report.get('user_id')}`",
        f"- Rejected candidates: {int(report.get('total_rejected') or 0)}",
        f"- Rows with later same-day outcomes: {int(report.get('with_later_outcomes') or 0)}",
        f"- Rows with forward close outcomes: {int(report.get('with_forward_outcomes') or 0)}",
        "",
    ]
    missing = report.get("missing_outcome_reasons")
    if isinstance(missing, Mapping) and missing:
        lines.append(
            "- Missing outcome reasons: "
            + ", ".join(f"{reason}={count}" for reason, count in sorted(missing.items()))
        )
        lines.append("")
    buckets = report.get("buckets") if isinstance(report.get("buckets"), Mapping) else {}
    for label in ("+5%", "+10%", "+20%"):
        rows = list(buckets.get(label) or [])
        lines.append(f"## Later Move {label}")
        if not rows:
            lines.extend(["", "No rejected candidates met this threshold.", ""])
            continue
        lines.extend(
            [
                "",
                "| Symbol | Return | High | Reject Price | Gain | RelVol | Spread | News | Catalyst | Reason |",
                "|---|---:|---:|---:|---:|---:|---:|---:|---:|---|",
            ]
        )
        for row in rows[:20]:
            lines.append(
                "| {symbol} | {ret:.2f}% | {high:.2f} | {price:.2f} | {gain:.2f}% | {rel:.2f} | {spread:.2f}% | {news} | {cat:.2f} | {reason} |".format(
                    symbol=row.get("symbol") or "",
                    ret=_safe_float(row.get("later_same_day_return_pct")),
                    high=_safe_float(row.get("later_same_day_high")),
                    price=_safe_float(row.get("price")),
                    gain=_safe_float(row.get("gain_pct")),
                    rel=_safe_float(row.get("rel_volume")),
                    spread=_safe_float(row.get("spread_pct")),
                    news=_safe_int(row.get("news_score")),
                    cat=_safe_float(row.get("catalyst_score")),
                    reason=str(row.get("rejection_reason") or "unknown"),
                )
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_dynamic_rejection_report(
    *,
    project_root: Path | str = ".",
    data_dir: Path | str = "data",
    user_id: str = "live_bot",
    day: str,
    history_dir: Path | str | None = None,
    bars_dir: Path | str | None = None,
) -> tuple[Path, Path, dict[str, Any]]:
    """Write markdown and JSON research outputs for rejected dynamic movers."""
    report = build_dynamic_rejection_report(
        data_dir=data_dir,
        user_id=user_id,
        day=day,
        history_dir=history_dir,
        bars_dir=bars_dir,
    )
    root = Path(project_root)
    out_dir = root / "reports" / "research_feedback"
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_user = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in str(user_id or "default"))
    md_path = out_dir / f"dynamic_rejections_{day}_{safe_user}.md"
    json_path = out_dir / f"dynamic_rejections_{day}_{safe_user}.json"
    md_path.write_text(render_dynamic_rejection_report(report), encoding="utf-8")
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return md_path, json_path, report
