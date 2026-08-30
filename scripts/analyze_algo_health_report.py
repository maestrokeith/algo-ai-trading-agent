#!/usr/bin/env python3
"""Root-cause analyzer for Level 1.5 algo health reports.

The script is intentionally read-only. It summarizes recent logs and runtime
artifacts so health issues explain likely causes without changing trading
behavior.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REASON_LABELS = {
    "unstable_quote": "unstable quote / spread too wide",
    "gain_above_max": "gain above max cap",
    "below_min_price": "below min price",
    "below_min_avg_volume": "below min average volume",
    "below_min_relative_volume": "below min relative volume",
    "entry_alignment": "entry alignment failure",
    "bad_quote": "bad quote",
    "catalyst_news_low": "catalyst/news score too low",
    "atr_guard": "ATR guard",
    "below_min_day_gain": "below min day gain",
    "above_max_price": "above max price",
    "intraday_range": "intraday range too low",
    "other": "other",
}


@dataclass(frozen=True)
class Rejection:
    symbol: str
    reason: str
    detail: str


def _load_json(path: Path) -> Any:
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None


def _latest_file_for_user(directory: Path, user_id: str) -> Path | None:
    if not directory.is_dir():
        return None
    files = sorted(
        directory.glob(f"*_{user_id}.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    return files[0] if files else None


def _fmt_number(value: Any, digits: int = 2) -> str | None:
    if value is None:
        return None
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return str(value)


def classify_reason(text: str) -> str:
    lowered = text.lower()
    if "unstable quote" in lowered or "spread too wide" in lowered or "spread_pct" in lowered:
        return "unstable_quote"
    if "gain filter" in lowered or "gain above" in lowered or "above max" in lowered:
        return "gain_above_max"
    if "below_min_price" in lowered or "below min price" in lowered:
        return "below_min_price"
    if "below_min_avg_volume" in lowered or "below min average volume" in lowered:
        return "below_min_avg_volume"
    if "below_min_relative_volume" in lowered or "relative_volume" in lowered or "min relative volume" in lowered:
        return "below_min_relative_volume"
    if "entry_alignment" in lowered or "need 5m breakout" in lowered:
        return "entry_alignment"
    if "bad quote" in lowered:
        return "bad_quote"
    if "catalyst" in lowered and ("score too low" in lowered or "news" in lowered):
        return "catalyst_news_low"
    if "atr expansion" in lowered or "atr guard" in lowered:
        return "atr_guard"
    if "below_min_day_gain" in lowered or "below min day gain" in lowered:
        return "below_min_day_gain"
    if "above_max_price" in lowered or "above max price" in lowered:
        return "above_max_price"
    if "intraday range" in lowered:
        return "intraday_range"
    return "other"


def _detail_from_candidate(candidate: dict[str, Any], reason_key: str) -> str:
    parts: list[str] = []
    field_map = {
        "price": "price",
        "min_price": "min",
        "max_price": "max",
        "gain_pct": "gain",
        "day_gain_pct": "gain",
        "max_day_gain_pct": "max",
        "avg_volume": "avg",
        "min_avg_volume": "min",
        "relative_volume": "rel",
        "rel_volume": "rel",
        "min_relative_volume": "min",
        "spread_pct": "spread",
        "max_spread_pct": "max",
        "catalyst_score": "catalyst_score",
        "news_score": "news_score",
    }
    seen_labels: set[str] = set()
    for field, label in field_map.items():
        if field not in candidate or label in seen_labels:
            continue
        value = _fmt_number(candidate.get(field))
        if value is not None:
            suffix = "%" if label in {"gain", "spread"} else ""
            parts.append(f"{label}={value}{suffix}")
            seen_labels.add(label)
    raw_reason = str(candidate.get("rejection_reason") or "")
    if raw_reason and not parts:
        parts.append(raw_reason)
    if reason_key == "entry_alignment" and raw_reason:
        match = re.search(r"\(got ([^)]+)\)", raw_reason)
        if match:
            parts.append(match.group(1))
    return " ".join(parts[:6])


def rejections_from_dynamic_artifact(path: Path | None) -> list[Rejection]:
    if path is None:
        return []
    data = _load_json(path)
    if not isinstance(data, dict):
        return []
    raw_rejections = data.get("rejected")
    if not isinstance(raw_rejections, list):
        return []
    rejections: list[Rejection] = []
    for item in raw_rejections:
        if not isinstance(item, dict):
            continue
        reason_text = str(item.get("rejection_reason") or item.get("reason") or "")
        reason_key = classify_reason(reason_text)
        symbol = str(item.get("symbol") or "unknown")
        rejections.append(Rejection(symbol=symbol, reason=reason_key, detail=_detail_from_candidate(item, reason_key)))
    return rejections


def rejections_from_journal(path: Path) -> list[Rejection]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    rejections: list[Rejection] = []
    patterns = [
        re.compile(r"DYNAMIC_SCAN reject (?P<symbol>[A-Z0-9._-]+): (?P<detail>.*)", re.IGNORECASE),
        re.compile(r"SKIP (?P<symbol>[A-Z0-9._-]+): reason=(?P<detail>.*)", re.IGNORECASE),
    ]
    for line in text.splitlines():
        for pattern in patterns:
            match = pattern.search(line)
            if not match:
                continue
            detail = match.group("detail").strip()
            rejections.append(
                Rejection(
                    symbol=match.group("symbol").upper(),
                    reason=classify_reason(detail),
                    detail=detail,
                )
            )
            break
    return rejections


def summarize_rejections(rejections: list[Rejection]) -> tuple[Counter[str], dict[str, list[Rejection]]]:
    counts: Counter[str] = Counter(item.reason for item in rejections)
    examples: dict[str, list[Rejection]] = defaultdict(list)
    seen: set[tuple[str, str]] = set()
    for item in rejections:
        key = (item.reason, item.symbol)
        if key in seen or len(examples[item.reason]) >= 5:
            continue
        examples[item.reason].append(item)
        seen.add(key)
    return counts, examples


def _count_json_collection(data: Any, key: str) -> int | None:
    if not isinstance(data, dict):
        return None
    value = data.get(key)
    if isinstance(value, list):
        return len(value)
    if isinstance(value, int):
        return value
    if isinstance(value, dict):
        return len(value)
    return None


def _artifact_age_minutes(path: Path) -> float | None:
    try:
        return (datetime.now(timezone.utc).timestamp() - path.stat().st_mtime) / 60.0
    except OSError:
        return None


def data_quality_signals(root: Path) -> list[str]:
    premarket = root / "data" / "premarket"
    lines: list[str] = []
    provider_data = _load_json(premarket / "provider_diagnostics_latest.json")
    if isinstance(provider_data, dict):
        providers = provider_data.get("providers")
        if isinstance(providers, dict):
            for name in ("newsapi", "alpaca", "sec", "earnings_overnight"):
                info = providers.get(name)
                if not isinstance(info, dict):
                    continue
                status = info.get("http_status")
                raw = info.get("raw_count")
                filtered = info.get("filtered_count")
                rate_limited = info.get("rate_limited")
                reason = info.get("reason")
                lines.append(
                    f"- {name}: status={status} raw_count={raw} filtered_count={filtered} "
                    f"rate_limited={rate_limited} reason={reason}"
                )
    news_data = _load_json(premarket / "news_diagnostics_latest.json")
    if isinstance(news_data, dict):
        lines.append(
            "- news diagnostics: provider={provider} status={status} raw_count={raw} "
            "filtered_count={filtered} reason={reason}".format(
                provider=news_data.get("provider"),
                status=news_data.get("http_status"),
                raw=news_data.get("raw_count"),
                filtered=news_data.get("filtered_count"),
                reason=news_data.get("reason"),
            )
        )
    for name, key in (
        ("latest_event_feed.json", "events"),
        ("latest_rankings.json", "rankings"),
        ("latest_catalysts.json", "catalysts"),
    ):
        path = premarket / name
        data = _load_json(path)
        count = _count_json_collection(data, key)
        if count is None and key == "rankings":
            count = _count_json_collection(data, "ranked_symbols")
        age = _artifact_age_minutes(path)
        age_text = "missing" if age is None else f"{age:.1f}m"
        lines.append(f"- {name}: age={age_text} {key}={count}")
        if isinstance(data, dict) and "catalyst_ranked_symbols" in data:
            lines.append(f"- catalyst_ranked_symbols={data.get('catalyst_ranked_symbols')}")
    return lines or ["- No premarket data-quality artifacts found."]


def trading_activity_context(root: Path, user_id: str) -> list[str]:
    lines: list[str] = []
    for directory in (root / "data" / "summaries", root / "data" / "summary"):
        latest = _latest_file_for_user(directory, user_id)
        if latest is None:
            continue
        data = _load_json(latest)
        if isinstance(data, dict):
            lines.append(f"- summary_artifact={latest}")
            for key in ("trades", "trade_count", "orders", "realized_pnl", "unrealized_pnl", "portfolio_value"):
                if key in data:
                    lines.append(f"- {key}={data.get(key)}")
            break
    replay = _latest_file_for_user(root / "data" / "replay", user_id) or _latest_file_for_user(
        root / "data" / "replay_market_session", user_id
    )
    if replay is not None:
        replay_data = _load_json(replay)
        lines.append(f"- replay_summary_available=true path={replay}")
        if isinstance(replay_data, dict):
            for key in ("submitted_orders", "simulated_submitted_orders", "orders", "fills", "allocator_input_missing"):
                if key in replay_data:
                    value = replay_data.get(key)
                    if isinstance(value, list):
                        value = len(value)
                    lines.append(f"- replay_{key}={value}")
    else:
        lines.append("- replay_summary_available=false")
    lines.append("- catalyst_outcomes_available=false")
    lines.append("- churn_signals_available=false")
    return lines


def interpretation(
    *,
    env_name: str,
    severity: str,
    kind: str,
    counts: Counter[str],
    data_quality: list[str],
) -> tuple[str, str]:
    total = sum(counts.values())
    unstable = counts["unstable_quote"]
    hard_quality = (
        unstable
        + counts["bad_quote"]
        + counts["below_min_price"]
        + counts["below_min_avg_volume"]
        + counts["gain_above_max"]
        + counts["above_max_price"]
    )
    provider_problem = any("rate_limited=True" in line or "raw_count=0" in line for line in data_quality)

    if "options" in kind.lower():
        return (
            "likely options pipeline inactivity",
            "Investigate paper options diagnostics and recent OPTION_ROUTE_CHECK/OPTION_SIGNAL logs before changing thresholds.",
        )
    if "replay" in kind.lower():
        return (
            "likely replay pipeline issue",
            "Inspect replay artifact generation and validation output for the paper environment.",
        )
    if total and hard_quality / total >= 0.55:
        return (
            "likely healthy filtering",
            "Most rejected symbols were filtered by quote quality, price, spread, or extreme gain guards. Monitor acceptance across multiple market days before changing gates.",
        )
    if "catalyst" in kind.lower() or "news" in kind.lower() or provider_problem:
        return (
            "likely news/catalyst coverage issue",
            "Investigate provider coverage, rate limits, and premarket artifact freshness.",
        )
    if total and (counts["below_min_relative_volume"] + counts["entry_alignment"]) / total >= 0.45:
        return (
            "possible over-filtering",
            "Review whether RVOL and entry-alignment filters are excluding later winners using research feedback before changing live rules.",
        )
    if total and unstable / total >= 0.35:
        return (
            "likely data-quality issue",
            "Review dynamic candidate source quality because unstable quote and spread rejects dominate.",
        )
    if severity == "research":
        return (
            "possible over-filtering",
            "Treat this as a research issue and compare rejected symbols against forward returns before changing scanner thresholds.",
        )
    return (
        "likely scanner health issue",
        "Inspect scanner artifacts, candidate source freshness, and recent logs for silent pipeline degradation.",
    )


def render_markdown(
    *,
    env_name: str,
    user_id: str,
    root: Path,
    journal_file: Path,
    severity: str,
    kind: str,
    detail: str,
) -> str:
    dynamic_file = _latest_file_for_user(root / "data" / "dynamic_scan_history", user_id)
    rejections = rejections_from_dynamic_artifact(dynamic_file)
    if not rejections:
        rejections = rejections_from_journal(journal_file)
    counts, examples = summarize_rejections(rejections)
    quality = data_quality_signals(root)
    activity = trading_activity_context(root, user_id)
    classification, next_action = interpretation(
        env_name=env_name,
        severity=severity,
        kind=kind,
        counts=counts,
        data_quality=quality,
    )

    lines: list[str] = [
        "## Root-Cause Analysis",
        "",
        f"- Analyzer Environment: {env_name}",
        f"- Analyzer User: {user_id}",
        f"- Analyzed Condition: {kind}",
        f"- Condition Detail: {detail}",
        f"- Dynamic Artifact: {dynamic_file if dynamic_file else 'not found'}",
        "",
        "### Rejection Summary",
        "",
    ]
    if counts:
        lines.append("Top rejection reasons:")
        lines.append("")
        for reason, count in counts.most_common():
            lines.append(f"- {REASON_LABELS.get(reason, reason)}: {count}")
    else:
        lines.append("No scanner rejection records found in the latest artifact or recent journal logs.")
    lines.extend(["", "### Representative Symbols", ""])
    if examples:
        for reason, items in sorted(examples.items(), key=lambda pair: counts[pair[0]], reverse=True):
            lines.append(f"{REASON_LABELS.get(reason, reason)}:")
            lines.append("")
            for item in items:
                detail_text = f" {item.detail}" if item.detail else ""
                lines.append(f"- {item.symbol}{detail_text}")
            lines.append("")
    else:
        lines.append("No representative rejected symbols available.")
        lines.append("")
    lines.extend(
        [
            "### Filter Quality Interpretation",
            "",
            f"{classification}.",
            "",
            "### Suggested Next Action",
            "",
            next_action,
            "",
            "### Data Quality Signals",
            "",
            *quality,
            "",
            "### Trading Activity Context",
            "",
            *activity,
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze algo health report root causes.")
    parser.add_argument("--root", required=True)
    parser.add_argument("--environment", required=True, choices=("LIVE", "PAPER"))
    parser.add_argument("--user", required=True)
    parser.add_argument("--journal-file", required=True)
    parser.add_argument("--severity", required=True)
    parser.add_argument("--kind", required=True)
    parser.add_argument("--detail", default="")
    args = parser.parse_args()

    print(
        render_markdown(
            env_name=args.environment,
            user_id=args.user,
            root=Path(args.root),
            journal_file=Path(args.journal_file),
            severity=args.severity,
            kind=args.kind,
            detail=args.detail,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
