"""Runtime health checks for the live trading loop."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class HealthCheckResult:
    """One health-check outcome."""

    name: str
    ok: bool
    reason: str = "ok"


def check_process_alive(*, process_start_ts: float, now_ts: float | None = None) -> HealthCheckResult:
    """Validate that the current process has a sane runtime clock."""
    now = time.time() if now_ts is None else float(now_ts)
    try:
        start = float(process_start_ts)
    except (TypeError, ValueError):
        return HealthCheckResult("process", False, "invalid_start_time")
    if start <= 0.0 or start != start:
        return HealthCheckResult("process", False, "invalid_start_time")
    if now + 1e-9 < start:
        return HealthCheckResult("process", False, "clock_moved_backward")
    return HealthCheckResult("process", True, "alive")


def check_broker_connectivity(broker: Any) -> HealthCheckResult:
    """Call a lightweight broker read to verify API connectivity."""
    for method_name in ("get_clock", "get_account_snapshot", "get_equity"):
        method = getattr(broker, method_name, None)
        if not callable(method):
            continue
        try:
            method()
            return HealthCheckResult("broker", True, method_name)
        except Exception as exc:
            return HealthCheckResult("broker", False, f"{method_name}:{type(exc).__name__}")
    return HealthCheckResult("broker", False, "no_health_read_method")


def check_news_pipeline(
    *,
    enabled: bool,
    pipeline: Any,
    rules: Any,
    summary: Mapping[str, Any] | None,
) -> HealthCheckResult:
    """Validate that the optional news pipeline is initialized when enabled."""
    if not enabled:
        return HealthCheckResult("news", True, "disabled")
    if pipeline is None:
        return HealthCheckResult("news", False, "pipeline_missing")
    if rules is None:
        return HealthCheckResult("news", False, "rules_missing")
    if not isinstance(summary, Mapping):
        return HealthCheckResult("news", False, "summary_missing")
    for key in ("articles_fetched", "articles_after_filter", "symbols_scored"):
        if key not in summary:
            return HealthCheckResult("news", False, f"summary_missing_{key}")
    return HealthCheckResult("news", True, "ready")


def evaluate_runtime_health(
    *,
    broker: Any,
    news_enabled: bool,
    news_pipeline: Any,
    news_rules: Any,
    news_summary: Mapping[str, Any] | None,
    process_start_ts: float,
    now_ts: float | None = None,
) -> list[HealthCheckResult]:
    """Run all live-loop health checks."""
    return [
        check_process_alive(process_start_ts=process_start_ts, now_ts=now_ts),
        check_broker_connectivity(broker),
        check_news_pipeline(
            enabled=bool(news_enabled),
            pipeline=news_pipeline,
            rules=news_rules,
            summary=news_summary,
        ),
    ]


def failed_health_checks(results: list[HealthCheckResult]) -> list[HealthCheckResult]:
    """Return failed checks only."""
    return [r for r in results if not r.ok]
