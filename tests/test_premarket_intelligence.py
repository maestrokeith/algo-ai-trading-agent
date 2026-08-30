from __future__ import annotations

import json
from datetime import datetime
from datetime import timedelta
import time
from zoneinfo import ZoneInfo

import src.premarket_intelligence as pm
from src.premarket_intelligence import (
    default_state_path,
    due_premarket_jobs,
    resolve_premarket_config,
    run_premarket_scheduler_startup_catchup,
)


def _cfg() -> dict:
    return {
        "premarket_intelligence": {
            "enabled": True,
            "keep_alive_overnight": True,
            "allow_trading": False,
            "news_scan_time": "05:00",
        },
        "universe": {"symbols": ["SPY", "NVDA"]},
    }


def test_missing_premarket_config_uses_disabled_defaults() -> None:
    resolved = resolve_premarket_config({})

    assert resolved.missing is True
    assert resolved.enabled is False
    assert resolved.keep_alive_overnight is False
    assert resolved.allow_trading is False
    assert resolved.news_scan_time == "05:15"


def test_startup_catchup_due_after_news_scan_time(tmp_path) -> None:
    now = datetime(2026, 6, 1, 6, 3, tzinfo=ZoneInfo("America/New_York"))

    assert due_premarket_jobs(_cfg(), now, state_path=default_state_path(tmp_path)) == ["news_5am"]
    results = run_premarket_scheduler_startup_catchup(
        _cfg(),
        now,
        project_root=tmp_path,
        dry_run=True,
    )

    assert results[0].job == "news_5am"
    assert results[0].due is True
    assert results[0].ran is False
    assert results[0].skipped_reason == "dry_run"
    assert results[0].symbols == 2
    assert results[0].news == 0
    assert results[0].filings == 0
    assert results[0].ranked == 0


def test_startup_catchup_not_due_before_news_scan_time(tmp_path) -> None:
    now = datetime(2026, 6, 1, 4, 3, tzinfo=ZoneInfo("America/New_York"))

    results = run_premarket_scheduler_startup_catchup(
        _cfg(),
        now,
        project_root=tmp_path,
        dry_run=True,
    )

    assert results[0].job == "news_5am"
    assert results[0].due is False
    assert results[0].ran is False
    assert results[0].skipped_reason == "not_due"


def test_news_5am_due_boundaries_until_completed(tmp_path) -> None:
    state_path = default_state_path(tmp_path)
    before = datetime(2026, 6, 1, 4, 59, tzinfo=ZoneInfo("America/New_York"))
    due_at_scan = datetime(2026, 6, 1, 5, 0, tzinfo=ZoneInfo("America/New_York"))
    after_scan = datetime(2026, 6, 1, 5, 1, tzinfo=ZoneInfo("America/New_York"))

    assert due_premarket_jobs(_cfg(), before, state_path=state_path) == []
    assert due_premarket_jobs(_cfg(), due_at_scan, state_path=state_path) == ["news_5am"]
    assert due_premarket_jobs(_cfg(), after_scan, state_path=state_path) == ["news_5am"]

    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps(
            {
                "news_5am:2026-06-01": {
                    "status": "done",
                    "started_at": "2026-06-01T05:00:00-04:00",
                    "finished_at": "2026-06-01T05:00:30-04:00",
                }
            }
        )
    )

    assert due_premarket_jobs(_cfg(), after_scan, state_path=state_path) == []


def test_news_5am_recovery_window_due_when_artifacts_missing(tmp_path) -> None:
    now = datetime(2026, 6, 1, 5, 1, tzinfo=ZoneInfo("America/New_York"))
    state_path = default_state_path(tmp_path)
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps(
            {
                "news_5am:2026-06-01": {
                    "status": "running",
                    "started_at": "2026-06-01T05:00:30-04:00",
                }
            }
        )
    )

    assert due_premarket_jobs(
        _cfg(),
        now,
        state_path=state_path,
        project_root=tmp_path,
    ) == ["news_5am"]


def test_execute_marks_running_then_done(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime(2026, 6, 1, 6, 3, tzinfo=ZoneInfo("America/New_York"))
    state_path = default_state_path(tmp_path)

    monkeypatch.setattr(
        pm,
        "execute_premarket_providers",
        lambda *a, **k: pm.PremarketProviderResults(
            newsapi=pm.ProviderExecResult("newsapi", request_sent=True, duration_ms=1.0, articles=0),
            alpaca=pm.ProviderExecResult("alpaca", request_sent=True, duration_ms=1.0, articles=0),
            sec=pm.ProviderExecResult("sec", request_sent=True, duration_ms=1.0, filings=0),
        ),
    )

    results = run_premarket_scheduler_startup_catchup(
        _cfg(),
        now,
        project_root=tmp_path,
        dry_run=False,
    )

    assert results[0].ran is True
    state = json.loads(state_path.read_text())
    row = state["news_5am:2026-06-01"]
    assert row["status"] == "done"
    assert row["finished_at"]
    assert "ran_at" not in row
    assert row["symbols"] == 2
    assert row["news_count"] == 0
    assert row["filings_count"] == 0


def test_scheduler_entrypoint_widens_universe_and_writes_non_core_catalyst(
    tmp_path,
    monkeypatch,
    caplog,
) -> None:
    import logging
    import src.premarket_intelligence as pm_mod

    caplog.set_level(logging.INFO)
    now = datetime(2026, 6, 1, 6, 3, tzinfo=ZoneInfo("America/New_York"))

    class FakeMarket:
        def get_top_movers(self):
            return [{"symbol": "DXST"}]

    def _newsapi(*_a, **_k):
        return pm_mod.ProviderExecResult(
            provider="newsapi",
            request_sent=True,
            articles=1,
            events=[
                pm_mod.NewsEvent(
                    symbol="DXST",
                    headline="DXST wins cloud deal",
                    source="newsapi",
                    score=6.5,
                )
            ],
        )

    def _empty_newsapi(*_a, **_k):
        return pm_mod.ProviderExecResult(provider="newsapi", request_sent=False, articles=0)

    monkeypatch.setenv("NEWSAPI_KEY", "secret-key")
    monkeypatch.setattr(pm_mod, "fetch_newsapi_articles", _newsapi)
    monkeypatch.setattr(pm_mod, "fetch_alpaca_news_events", _empty_newsapi)
    monkeypatch.setattr(pm_mod, "fetch_sec_filings", lambda *a, **k: pm_mod.ProviderExecResult(provider="sec", request_sent=False, filings=0))
    monkeypatch.setattr(pm_mod, "fetch_benzinga_events", _empty_newsapi)
    monkeypatch.setattr(pm_mod, "fetch_twitter_trusted_events", _empty_newsapi)
    monkeypatch.setattr(pm_mod, "fetch_overnight_earnings_events", _empty_newsapi)
    pm_mod._NEWSAPI_QUERY_CACHE.clear()
    pm_mod._NEWSAPI_DAILY_CALLS.clear()
    pm_mod._PREMARKET_PROVIDER_CACHE.clear()

    config = {
        "premarket_intelligence": {
            "enabled": True,
            "keep_alive_overnight": True,
            "allow_trading": False,
            "news_scan_time": "05:00",
        },
        "universe": {"symbols": ["AAPL", "SPY"]},
        "news_sentiment": {"enabled": True},
    }

    pm_mod.log_premarket_startup_config(config)
    results = pm_mod.run_premarket_scheduler_tick(
        config,
        now,
        project_root=tmp_path,
        market_client=FakeMarket(),
        reason="manual_debug",
        dry_run=True,
        force_jobs=["news_5am"],
    )

    catalyst_path = pm_mod.default_premarket_catalysts_path(tmp_path)
    payload = json.loads(catalyst_path.read_text())
    assert results[0].job == "news_5am"
    assert "PREMARKET_PIPELINE_VERSION widened_universe=true" in caplog.text
    assert "PREMARKET_CANDIDATE_UNIVERSE count=" in caplog.text
    assert "PREMARKET_NEWS_HIT symbol=DXST articles=1" in caplog.text
    assert "PREMARKET_CATALYST_WRITTEN symbol=DXST score=" in caplog.text
    assert payload["catalysts"]
    assert payload["catalysts"][0]["symbol"] == "DXST"
    assert payload["rankings"]


def test_reuters_nvda_vera_cpu_story_is_ranked_above_package_release() -> None:
    now = datetime(2026, 6, 12, 8, 0, tzinfo=ZoneInfo("America/New_York"))

    rankings = pm.build_premarket_rankings(
        ["NVDA"],
        catalysts={},
        events=[
            pm.NewsEvent(
                symbol="NVDA",
                headline="Nvidia Vera CPU roadmap points to faster AI server chips",
                source="newsapi",
                publisher="Reuters",
                url="https://www.reuters.com/technology/",
                published_at="2026-06-12T11:30:00Z",
            ),
            pm.NewsEvent(
                symbol="NVDA",
                headline="NVDA 0.4.1 package released on PyPI",
                source="newsapi",
                publisher="PyPI",
                url="https://pypi.org/project/nvda/",
                published_at="2026-06-12T11:35:00Z",
            ),
        ],
        cfg={"ai_news_ranking": {"enabled": True, "score_weight": 2.0}},
        now=now,
    )

    assert rankings
    assert rankings[0].symbol == "NVDA"
    assert rankings[0].catalyst_type == "product"
    assert rankings[0].publisher == "Reuters"
    assert rankings[0].score > 7.0


def test_newsapi_package_release_article_filtered_before_event_creation(caplog) -> None:
    caplog.set_level("INFO", logger="src.premarket_intelligence")

    events = pm._newsapi_article_to_events(
        [
            {
                "title": "NVDA 0.4.1 package released on PyPI",
                "description": "Python package metadata update.",
                "source": {"name": "PyPI"},
                "url": "https://pypi.org/project/nvda/",
                "_premarket_symbol": "NVDA",
            }
        ],
        ["NVDA"],
        {},
    )

    assert events == []
    assert "NEWS_PACKAGE_SPAM_FILTERED symbol=NVDA source=newsapi publisher=PyPI" in caplog.text


def test_startup_catchup_writes_hpe_catalyst_and_dynamic_scan_reads_it(
    tmp_path,
    monkeypatch,
) -> None:
    import src.app.live_cycle as lc
    import src.dynamic_universe as du

    now = datetime(2026, 6, 1, 6, 3, tzinfo=ZoneInfo("America/New_York"))

    class FakeMarket:
        def __init__(self) -> None:
            self._news = object()

        def get_top_movers(self):
            return [{"symbol": "HPE"}]

        def get_recent_news(self, *args, **kwargs):
            return [
                (
                    "data",
                    {
                        "news": [
                            {
                                "headline": "HPE wins AI infrastructure deal",
                                "symbols": ["HPE"],
                                "created_at": "2026-06-01T09:30:00Z",
                            }
                        ]
                    },
                ),
                ("next_page_token", None),
            ]

        def get_snapshots_batch(self, symbols):
            return {
                "HPE": {
                    "price": 18.0,
                    "day_gain_pct": 9.0,
                    "volume": 2_000_000,
                    "bid": 17.95,
                    "ask": 18.05,
                }
            }

        def get_avg_volumes(self, symbols):
            return {"HPE": 1_000_000.0}

        def get_bars_batch(self, symbols, timeframe: str = "1Min", limit: int = 60):
            import pandas as pd

            bars = pd.DataFrame(
                {
                    "high": [18.0, 18.1, 18.2, 18.3],
                    "low": [17.8, 17.9, 18.0, 18.1],
                    "close": [17.95, 18.0, 18.1, 18.2],
                    "volume": [100_000, 100_000, 100_000, 100_000],
                }
            )
            return {sym: bars for sym in symbols}

        def get_snapshot(self, symbol: str):
            return self.get_snapshots_batch([symbol]).get(symbol, {})

        def get_avg_volume(self, symbol: str) -> float:
            return self.get_avg_volumes([symbol]).get(symbol, 1.0)

        def get_top_movers(self):
            return [{"symbol": "HPE"}]

    def _empty(*_a, **_k):
        return pm.ProviderExecResult(provider="newsapi", request_sent=False, articles=0)

    monkeypatch.setattr(pm, "fetch_newsapi_articles", _empty)
    monkeypatch.setattr(pm, "fetch_sec_filings", lambda *a, **k: pm.ProviderExecResult(provider="sec", request_sent=False, filings=0))
    monkeypatch.setattr(pm, "fetch_benzinga_events", _empty)
    monkeypatch.setattr(pm, "fetch_twitter_trusted_events", _empty)
    monkeypatch.setattr(pm, "fetch_overnight_earnings_events", _empty)
    pm._NEWSAPI_QUERY_CACHE.clear()
    pm._NEWSAPI_DAILY_CALLS.clear()
    pm._PREMARKET_PROVIDER_CACHE.clear()

    config = {
        "premarket_intelligence": {
            "enabled": True,
            "keep_alive_overnight": True,
            "allow_trading": False,
            "news_scan_time": "05:00",
        },
        "universe": {"symbols": ["AAPL", "SPY"]},
        "news_sentiment": {"enabled": True},
        "dynamic_universe": {
            "enabled": True,
            "max_symbols": 3,
            "min_price": 2,
            "max_price": 120,
            "min_day_gain_pct": 8.0,
            "max_day_gain_pct": 80.0,
            "min_avg_volume": 10_000,
            "min_relative_volume": 0.75,
            "min_rel_volume": 0.75,
            "max_spread_pct": 5.0,
            "catalyst_boost": {
                "enabled": True,
                "min_news_score": 0.60,
                "score_boost": 2.0,
                "allow_rel_volume_relax": True,
                "min_relative_volume_with_catalyst": 0.75,
                "allow_vwap_relax": True,
                "max_gain_pct_with_catalyst": 120,
            },
        },
    }

    pm.log_premarket_startup_config(config)
    pm.run_premarket_scheduler_startup_catchup(
        config,
        now,
        project_root=tmp_path,
        market_client=FakeMarket(),
        dry_run=False,
        force_jobs=["news_5am"],
    )
    loaded = lc._load_premarket_artifacts_into_runtime(
        engine=type("E", (), {})(),
        project_root=tmp_path,
        now=now,
    )
    result = du.scan_candidates_batch(
        FakeMarket(),
        [],
        config["dynamic_universe"],
        emit_logs=False,
        premarket_artifacts=loaded,
    )

    assert "HPE" in loaded
    assert loaded["HPE"]["event_score"] > 0
    assert result.accepted[0].symbol == "HPE"
    assert result.accepted[0].catalyst_score > 0


def test_stale_running_job_is_due_for_rerun(tmp_path) -> None:
    now = datetime(2026, 6, 1, 6, 3, tzinfo=ZoneInfo("America/New_York"))
    state_path = default_state_path(tmp_path)
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps(
            {
                "news_5am:2026-06-01": {
                    "status": "running",
                    "started_at": (now - timedelta(minutes=3)).isoformat(),
                }
            }
        )
    )

    assert due_premarket_jobs(_cfg(), now, state_path=state_path) == ["news_5am"]
    row = json.loads(state_path.read_text())["news_5am:2026-06-01"]
    assert row["status"] == "stale"
    assert row["stale_reason"] == "running_older_than_120s"


def test_recent_running_job_is_not_due(tmp_path) -> None:
    now = datetime(2026, 6, 1, 6, 3, tzinfo=ZoneInfo("America/New_York"))
    state_path = default_state_path(tmp_path)
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps(
            {
                "news_5am:2026-06-01": {
                    "status": "running",
                    "started_at": (now - timedelta(seconds=30)).isoformat(),
                }
            }
        )
    )

    assert due_premarket_jobs(_cfg(), now, state_path=state_path) == []


def test_timeout_marks_error_not_done(tmp_path, monkeypatch) -> None:
    now = datetime(2026, 6, 1, 6, 3, tzinfo=ZoneInfo("America/New_York"))
    cfg = _cfg()
    cfg["premarket_intelligence"]["job_timeout_seconds"] = 1

    def slow_job(*args, **kwargs):
        time.sleep(2)
        return pm._PremarketJobStats(symbols=2)

    monkeypatch.setattr(pm, "_run_news_5am_job", slow_job)

    results = run_premarket_scheduler_startup_catchup(
        cfg,
        now,
        project_root=tmp_path,
        dry_run=False,
    )

    assert results[0].ran is False
    assert results[0].skipped_reason == "error"
    assert "Timeout" in results[0].error
    row = json.loads(default_state_path(tmp_path).read_text())["news_5am:2026-06-01"]
    assert row["status"] == "failed"
    assert row["finished_at"]
    assert "Timeout" in row["error"]


def test_legacy_ran_at_without_status_is_due_for_rerun(
    tmp_path, caplog
) -> None:
    import logging

    now = datetime(2026, 6, 1, 6, 12, tzinfo=ZoneInfo("America/New_York"))
    state_path = default_state_path(tmp_path)
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps(
            {
                "news_5am:2026-06-01": {
                    "ran_at": "2026-06-01T06:12:11-04:00",
                    "reason": "startup_catchup",
                    "symbols": 35,
                }
            }
        )
    )

    caplog.set_level(logging.WARNING)
    assert due_premarket_jobs(_cfg(), now, state_path=state_path) == ["news_5am"]
    assert "PREMARKET_JOB_STALE_RERUN job=news_5am reason=legacy_ran_at_without_done" in caplog.text


def test_done_with_finished_at_is_not_due(tmp_path) -> None:
    now = datetime(2026, 6, 1, 6, 12, tzinfo=ZoneInfo("America/New_York"))
    state_path = default_state_path(tmp_path)
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps(
            {
                "news_5am:2026-06-01": {
                    "status": "done",
                    "finished_at": "2026-06-01T06:15:00-04:00",
                    "started_at": "2026-06-01T06:12:00-04:00",
                    "symbols": 35,
                    "news_count": 10,
                    "filings_count": 0,
                }
            }
        )
    )

    assert due_premarket_jobs(_cfg(), now, state_path=state_path) == []


def test_dry_run_skips_provider_calls(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    calls = {"n": 0}

    def _providers(*_a, **_k):
        calls["n"] += 1
        raise AssertionError("providers should not run in dry_run")

    monkeypatch.setattr(pm, "execute_premarket_providers", _providers)
    now = datetime(2026, 6, 1, 6, 3, tzinfo=ZoneInfo("America/New_York"))
    pm._run_news_5am_job(_cfg(), now, dry_run=True, manual_debug=False)
    assert calls["n"] == 0


def test_manual_debug_runs_providers_in_dry_run(monkeypatch: pytest.MonkeyPatch) -> None:
    called = {"ok": False}

    def _providers(*_a, **_k):
        called["ok"] = True
        return pm.PremarketProviderResults(
            newsapi=pm.ProviderExecResult("newsapi", request_sent=True, duration_ms=12.0, articles=2),
            alpaca=pm.ProviderExecResult("alpaca", request_sent=True, duration_ms=8.0, articles=1),
            sec=pm.ProviderExecResult("sec", request_sent=True, duration_ms=5.0, filings=0),
        )

    monkeypatch.setattr(pm, "execute_premarket_providers", _providers)
    now = datetime(2026, 6, 1, 6, 3, tzinfo=ZoneInfo("America/New_York"))
    stats = pm._run_news_5am_job(_cfg(), now, dry_run=True, manual_debug=True)
    assert called["ok"] is True
    assert stats.news == 3


def test_job_duration_positive_when_providers_called(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    def _providers(*_a, **_k):
        return pm.PremarketProviderResults(
            newsapi=pm.ProviderExecResult("newsapi", request_sent=True, duration_ms=25.0, articles=2),
            alpaca=pm.ProviderExecResult("alpaca", request_sent=True, duration_ms=15.0, articles=1),
            sec=pm.ProviderExecResult("sec", request_sent=True, duration_ms=10.0, filings=0),
        )

    monkeypatch.setattr(pm, "execute_premarket_providers", _providers)
    now = datetime(2026, 6, 1, 6, 3, tzinfo=ZoneInfo("America/New_York"))
    results = pm.run_premarket_scheduler_tick(
        _cfg(),
        now,
        project_root=tmp_path,
        dry_run=False,
        force_jobs=["news_5am"],
    )
    assert results[0].news == 3
    assert results[0].ran is True
