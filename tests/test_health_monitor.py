"""Tests for runtime health monitoring."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from src.health_monitor import (
    check_broker_connectivity,
    check_news_pipeline,
    check_process_alive,
    evaluate_runtime_health,
    failed_health_checks,
)


def test_check_process_alive_ok() -> None:
    result = check_process_alive(process_start_ts=100.0, now_ts=120.0)
    assert result.ok is True
    assert result.reason == "alive"


def test_check_process_alive_detects_bad_clock() -> None:
    result = check_process_alive(process_start_ts=200.0, now_ts=120.0)
    assert result.ok is False
    assert result.reason == "clock_moved_backward"


def test_check_broker_connectivity_uses_available_read() -> None:
    broker = SimpleNamespace(get_equity=MagicMock(return_value=100_000.0))
    result = check_broker_connectivity(broker)
    assert result.ok is True
    assert result.reason == "get_equity"
    broker.get_equity.assert_called_once()


def test_check_broker_connectivity_reports_failure() -> None:
    broker = SimpleNamespace(get_clock=MagicMock(side_effect=RuntimeError("down")))
    result = check_broker_connectivity(broker)
    assert result.ok is False
    assert result.reason == "get_clock:RuntimeError"


def test_check_news_pipeline_disabled_is_ok() -> None:
    result = check_news_pipeline(enabled=False, pipeline=None, rules=None, summary=None)
    assert result.ok is True
    assert result.reason == "disabled"


def test_check_news_pipeline_enabled_requires_components() -> None:
    result = check_news_pipeline(enabled=True, pipeline=None, rules=object(), summary={})
    assert result.ok is False
    assert result.reason == "pipeline_missing"


def test_evaluate_runtime_health_returns_failures() -> None:
    broker = SimpleNamespace(get_equity=MagicMock(return_value=1.0))
    results = evaluate_runtime_health(
        broker=broker,
        news_enabled=True,
        news_pipeline=object(),
        news_rules=None,
        news_summary={
            "articles_fetched": 0,
            "articles_after_filter": 0,
            "symbols_scored": 0,
        },
        process_start_ts=100.0,
        now_ts=120.0,
    )

    failures = failed_health_checks(results)
    assert [f.name for f in failures] == ["news"]
    assert failures[0].reason == "rules_missing"
