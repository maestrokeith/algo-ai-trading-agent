from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import scripts.run_premarket_collection as collection
import src.premarket_intelligence as pm
from src.news_catalyst import NewsCatalyst


def test_premarket_collection_skips_outside_window(monkeypatch, tmp_path: Path, capsys) -> None:
    calls = {"tick": 0}

    monkeypatch.setattr(
        collection,
        "load_app_config",
        lambda _path: {
            "premarket_intelligence": {
                "enabled": True,
                "collection_start_time": "05:15",
                "collection_end_time": "09:25",
            }
        },
    )
    monkeypatch.setattr(collection, "log_premarket_startup_config", lambda _cfg: type("PM", (), {"enabled": True})())

    def _tick(*_args, **_kwargs):
        calls["tick"] += 1
        return []

    monkeypatch.setattr(collection, "run_premarket_scheduler_tick", _tick)

    rc = collection.main(["--project-root", str(tmp_path), "--now", "2026-06-01T04:30:00-04:00"])

    assert rc == 0
    assert calls["tick"] == 0
    assert "PREMARKET_COLLECTION_SKIP reason=outside_window" in capsys.readouterr().out


def test_premarket_collection_forces_refresh_inside_window(monkeypatch, tmp_path: Path) -> None:
    captured = {}

    monkeypatch.setattr(
        collection,
        "load_app_config",
        lambda _path: {"premarket_intelligence": {"enabled": True, "allow_trading": True}},
    )
    monkeypatch.setattr(collection, "log_premarket_startup_config", lambda _cfg: type("PM", (), {"enabled": True})())

    def _tick(config, now, **kwargs):
        captured["allow_trading"] = config["premarket_intelligence"]["allow_trading"]
        captured["now"] = now
        captured["kwargs"] = kwargs
        return [
            type(
                "Result",
                (),
                {
                    "job": "news_5am",
                    "due": True,
                    "ran": True,
                    "ranked": 2,
                    "news": 3,
                    "filings": 1,
                    "skipped_reason": "",
                    "reason": "premarket_collection",
                    "error": None,
                },
            )()
        ]

    monkeypatch.setattr(collection, "run_premarket_scheduler_tick", _tick)

    rc = collection.main(["--project-root", str(tmp_path), "--now", "2026-06-01T05:20:00-04:00"])

    assert rc == 0
    assert captured["allow_trading"] is False
    assert captured["kwargs"]["reason"] == "premarket_collection"
    assert captured["kwargs"]["dry_run"] is False
    assert captured["kwargs"]["force_jobs"] == ["news_5am"]
    assert captured["now"].isoformat() == "2026-06-01T05:20:00-04:00"


def test_bin_algo_exposes_premarket_refresh_and_prefers_repo_venv() -> None:
    wrapper = (Path(__file__).resolve().parents[1] / "bin" / "algo").read_text(encoding="utf-8")

    assert 'if [[ -n "${PYTHON:-}" ]]; then' in wrapper
    assert 'elif [[ -x "$ROOT/.venv/bin/python" ]]; then' in wrapper
    assert 'PY="$ROOT/.venv/bin/python"' in wrapper
    assert "premarket-refresh)" in wrapper
    assert 'exec "$PY" scripts/run_premarket_collection.py "$@"' in wrapper


def _enabled_config() -> dict:
    return {
        "premarket_intelligence": {
            "enabled": True,
            "allow_trading": True,
            "job_timeout_seconds": 5,
        },
        "universe": {"symbols": ["AAPL", "MSFT"]},
    }


def _enabled_pm() -> object:
    return type("PM", (), {"enabled": True})()


def test_premarket_collection_zero_news_exits_cleanly_and_writes_artifacts(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    monkeypatch.setattr(collection, "load_app_config", lambda _path: _enabled_config())
    monkeypatch.setattr(collection, "log_premarket_startup_config", lambda _cfg: _enabled_pm())
    monkeypatch.setattr(
        pm,
        "execute_premarket_providers",
        lambda *_args, **_kwargs: pm.PremarketProviderResults(
            newsapi=pm.ProviderExecResult("newsapi", request_sent=True, articles=0),
            alpaca=pm.ProviderExecResult("alpaca", request_sent=True, articles=0),
            sec=pm.ProviderExecResult("sec", request_sent=True, filings=0),
        ),
    )

    rc = collection.main(["--project-root", str(tmp_path), "--force", "--now", "2026-06-01T05:20:00-04:00"])

    out = capsys.readouterr().out
    assert rc == 0
    assert "PREMARKET_COLLECTION_RESULT job=news_5am ran=true persisted=true ranked=0 news=0 filings=0" in out
    assert pm.default_premarket_event_feed_path(tmp_path).exists()
    assert pm.default_premarket_rankings_path(tmp_path).exists()
    assert pm.default_premarket_catalysts_path(tmp_path).exists()


def test_premarket_collection_rate_limited_newsapi_logs_result_without_crash(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    monkeypatch.setattr(collection, "load_app_config", lambda _path: _enabled_config())
    monkeypatch.setattr(collection, "log_premarket_startup_config", lambda _cfg: _enabled_pm())
    monkeypatch.setattr(
        pm,
        "execute_premarket_providers",
        lambda *_args, **_kwargs: pm.PremarketProviderResults(
            newsapi=pm.ProviderExecResult(
                "newsapi",
                request_sent=True,
                articles=0,
                raw_articles_before_filter=0,
                articles_after_filter=0,
                http_status=429,
                skip_reason="rate_limited",
                error="rate limited",
            ),
            alpaca=pm.ProviderExecResult("alpaca", request_sent=True, articles=0, http_status=200),
            sec=pm.ProviderExecResult("sec", request_sent=False, filings=0),
            overnight_earnings=pm.ProviderExecResult(
                "earnings_overnight",
                request_sent=False,
                http_status=429,
                skip_reason="depends_on_newsapi_rate_limited",
            ),
        ),
    )

    rc = collection.main(["--project-root", str(tmp_path), "--force", "--now", "2026-06-01T05:20:00-04:00"])

    out = capsys.readouterr().out
    diagnostics = json.loads(pm.default_premarket_provider_diagnostics_path(tmp_path).read_text(encoding="utf-8"))
    assert rc == 0
    assert "PREMARKET_COLLECTION_RESULT job=news_5am ran=true persisted=true ranked=0 news=0 filings=0" in out
    assert diagnostics["providers"]["newsapi"]["http_status"] == 429
    assert diagnostics["providers"]["newsapi"]["rate_limited"] is True
    assert diagnostics["providers"]["alpaca"]["http_status"] == 200
    assert diagnostics["providers"]["alpaca"]["raw_count"] == 0
    assert diagnostics["providers"]["earnings_overnight"]["reason"] == "depends_on_newsapi_rate_limited"


def test_empty_latest_artifacts_preserve_richer_fresh_existing(tmp_path: Path, caplog) -> None:
    now = datetime(2026, 6, 1, 5, 20, tzinfo=ZoneInfo("America/New_York"))
    event = pm.NewsEvent(symbol="AAPL", headline="AAPL catalyst", source="newsapi", score=7.0)
    catalyst = NewsCatalyst("AAPL", 7, "AAPL catalyst", source="newsapi", catalyst_type="ai")
    ranking = pm.PremarketRankEntry("AAPL", 7.0, "ai", "newsapi", 0.9, "ai catalyst")

    pm.write_premarket_artifacts(
        tmp_path,
        now=now,
        source="news_5am",
        events=[event],
        catalysts={"AAPL": catalyst},
        rankings=[ranking],
        ttl_minutes=390,
    )
    previous_path = tmp_path / "data" / "premarket" / "previous_non_empty_rankings.json"
    previous_payload = json.loads(previous_path.read_text(encoding="utf-8"))
    assert previous_payload["rankings"][0]["symbol"] == "AAPL"

    pm.write_premarket_artifacts(
        tmp_path,
        now=now,
        source="news_5am",
        events=[],
        catalysts={},
        rankings=[],
        ttl_minutes=390,
        config={"premarket_intelligence": {"min_events_to_overwrite": 1, "min_rankings_to_overwrite": 1}},
    )

    latest_payload = json.loads(pm.default_premarket_rankings_path(tmp_path).read_text(encoding="utf-8"))
    preserved_payload = json.loads(previous_path.read_text(encoding="utf-8"))
    assert latest_payload["rankings"][0]["symbol"] == "AAPL"
    assert preserved_payload["rankings"][0]["symbol"] == "AAPL"
    assert "PREMARKET_ARTIFACT_PRESERVED reason=low_coverage_or_rate_limited" in caplog.text


def test_empty_latest_artifacts_allowed_without_richer_existing(tmp_path: Path) -> None:
    now = datetime(2026, 6, 1, 5, 20, tzinfo=ZoneInfo("America/New_York"))

    pm.write_premarket_artifacts(
        tmp_path,
        now=now,
        source="news_5am",
        events=[],
        catalysts={},
        rankings=[],
        ttl_minutes=390,
    )

    latest_payload = json.loads(pm.default_premarket_rankings_path(tmp_path).read_text(encoding="utf-8"))
    assert latest_payload["rankings"] == []
    assert latest_payload["events"] == []


def test_healthy_richer_refresh_overwrites_old_artifacts(tmp_path: Path) -> None:
    now = datetime(2026, 6, 1, 5, 20, tzinfo=ZoneInfo("America/New_York"))
    old_event = pm.NewsEvent(symbol="AAPL", headline="AAPL catalyst", source="newsapi", score=7.0)
    old_catalyst = NewsCatalyst("AAPL", 7, "AAPL catalyst", source="newsapi", catalyst_type="ai")
    old_ranking = pm.PremarketRankEntry("AAPL", 7.0, "ai", "newsapi", 0.9, "ai catalyst")
    pm.write_premarket_artifacts(
        tmp_path,
        now=now - timedelta(minutes=10),
        source="news_5am",
        events=[old_event],
        catalysts={"AAPL": old_catalyst},
        rankings=[old_ranking],
        ttl_minutes=390,
        config={"premarket_intelligence": {"min_events_to_overwrite": 1, "min_rankings_to_overwrite": 1}},
    )
    new_events = [
        pm.NewsEvent(symbol="MSFT", headline="MSFT catalyst", source="newsapi", score=8.0),
        pm.NewsEvent(symbol="NVDA", headline="NVDA catalyst", source="newsapi", score=8.0),
    ]
    new_catalysts = {
        "MSFT": NewsCatalyst("MSFT", 8, "MSFT catalyst", source="newsapi", catalyst_type="ai"),
        "NVDA": NewsCatalyst("NVDA", 8, "NVDA catalyst", source="newsapi", catalyst_type="ai"),
    }
    new_rankings = [
        pm.PremarketRankEntry("MSFT", 8.0, "ai", "newsapi", 0.9, "ai catalyst"),
        pm.PremarketRankEntry("NVDA", 8.0, "ai", "newsapi", 0.9, "ai catalyst"),
    ]

    pm.write_premarket_artifacts(
        tmp_path,
        now=now,
        source="news_5am",
        events=new_events,
        catalysts=new_catalysts,
        rankings=new_rankings,
        ttl_minutes=390,
        config={"premarket_intelligence": {"min_events_to_overwrite": 2, "min_rankings_to_overwrite": 2}},
    )

    latest_payload = json.loads(pm.default_premarket_rankings_path(tmp_path).read_text(encoding="utf-8"))
    assert [row["symbol"] for row in latest_payload["rankings"]] == ["MSFT", "NVDA"]


def test_stale_richer_artifacts_do_not_block_current_thin_refresh(tmp_path: Path) -> None:
    now = datetime(2026, 6, 1, 5, 20, tzinfo=ZoneInfo("America/New_York"))
    old_event = pm.NewsEvent(symbol="AAPL", headline="AAPL catalyst", source="newsapi", score=7.0)
    old_catalyst = NewsCatalyst("AAPL", 7, "AAPL catalyst", source="newsapi", catalyst_type="ai")
    old_ranking = pm.PremarketRankEntry("AAPL", 7.0, "ai", "newsapi", 0.9, "ai catalyst")
    pm.write_premarket_artifacts(
        tmp_path,
        now=now - timedelta(minutes=20),
        source="news_5am",
        events=[old_event],
        catalysts={"AAPL": old_catalyst},
        rankings=[old_ranking],
        ttl_minutes=1,
        config={"premarket_intelligence": {"min_events_to_overwrite": 1, "min_rankings_to_overwrite": 1}},
    )
    new_event = pm.NewsEvent(symbol="NVDA", headline="NVDA 424B5", source="sec", score=3.0, form="424B5")
    new_catalyst = NewsCatalyst("NVDA", 3, "NVDA 424B5", source="sec", catalyst_type="sec_filing")
    new_ranking = pm.PremarketRankEntry("NVDA", 3.0, "sec_filing", "sec_filing", 0.7, "sec filing", form="424B5")

    pm.write_premarket_artifacts(
        tmp_path,
        now=now,
        source="news_5am",
        events=[new_event],
        catalysts={"NVDA": new_catalyst},
        rankings=[new_ranking],
        ttl_minutes=390,
        config={"premarket_intelligence": {"min_events_to_overwrite": 2, "min_rankings_to_overwrite": 2}},
        provider_rate_limited=True,
    )

    latest_payload = json.loads(pm.default_premarket_rankings_path(tmp_path).read_text(encoding="utf-8"))
    assert [row["symbol"] for row in latest_payload["rankings"]] == ["NVDA"]


def test_newsapi_429_thin_collection_preserves_richer_existing(
    monkeypatch,
    tmp_path: Path,
    capsys,
    caplog,
) -> None:
    config = _enabled_config()
    config["premarket_intelligence"].update(
        {
            "min_events_to_overwrite": 2,
            "min_rankings_to_overwrite": 2,
            "preserve_on_provider_rate_limit": True,
            "preserve_existing_if_richer": True,
        }
    )
    monkeypatch.setattr(collection, "load_app_config", lambda _path: config)
    monkeypatch.setattr(collection, "log_premarket_startup_config", lambda _cfg: _enabled_pm())
    now = datetime(2026, 6, 1, 5, 20, tzinfo=ZoneInfo("America/New_York"))
    rich_events = [
        pm.NewsEvent(symbol="MSFT", headline="MSFT catalyst", source="newsapi", score=8.0),
        pm.NewsEvent(symbol="NVDA", headline="NVDA catalyst", source="newsapi", score=8.0),
    ]
    rich_catalysts = {
        "MSFT": NewsCatalyst("MSFT", 8, "MSFT catalyst", source="newsapi", catalyst_type="ai"),
        "NVDA": NewsCatalyst("NVDA", 8, "NVDA catalyst", source="newsapi", catalyst_type="ai"),
    }
    rich_rankings = [
        pm.PremarketRankEntry("MSFT", 8.0, "ai", "newsapi", 0.9, "ai catalyst"),
        pm.PremarketRankEntry("NVDA", 8.0, "ai", "newsapi", 0.9, "ai catalyst"),
    ]
    pm.write_premarket_artifacts(
        tmp_path,
        now=now - timedelta(minutes=5),
        source="news_5am",
        events=rich_events,
        catalysts=rich_catalysts,
        rankings=rich_rankings,
        ttl_minutes=390,
        config=config,
    )
    thin_event = pm.NewsEvent(symbol="NVDA", headline="NVDA 424B5", source="sec", score=3.0, form="424B5")
    thin_catalyst = NewsCatalyst("NVDA", 3, "NVDA 424B5", source="sec", catalyst_type="sec_filing")
    thin_ranking = pm.PremarketRankEntry("NVDA", 3.0, "sec_filing", "sec_filing", 0.7, "sec filing", form="424B5")
    monkeypatch.setattr(
        pm,
        "execute_premarket_providers",
        lambda *_args, **_kwargs: pm.PremarketProviderResults(
            newsapi=pm.ProviderExecResult(
                "newsapi",
                request_sent=True,
                articles=0,
                http_status=429,
                skip_reason="rate_limited",
            ),
            alpaca=pm.ProviderExecResult("alpaca", request_sent=True, articles=0, http_status=200),
            sec=pm.ProviderExecResult("sec", request_sent=True, filings=1, events=[thin_event]),
            events=[thin_event],
            catalysts={"NVDA": thin_catalyst},
            rankings=[thin_ranking],
        ),
    )

    rc = collection.main(["--project-root", str(tmp_path), "--force", "--now", now.isoformat()])

    capsys.readouterr()
    latest_payload = json.loads(pm.default_premarket_rankings_path(tmp_path).read_text(encoding="utf-8"))
    diagnostics = json.loads(pm.default_premarket_provider_diagnostics_path(tmp_path).read_text(encoding="utf-8"))
    assert rc == 0
    assert [row["symbol"] for row in latest_payload["rankings"]] == ["MSFT", "NVDA"]
    assert diagnostics["providers"]["newsapi"]["rate_limited"] is True
    assert diagnostics["providers"]["newsapi"]["reason"] == "rate_limited"
    assert "PREMARKET_ARTIFACT_PRESERVED reason=low_coverage_or_rate_limited" in caplog.text


def test_premarket_collection_successful_provider_run_persists_ranked_artifacts(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    monkeypatch.setattr(collection, "load_app_config", lambda _path: _enabled_config())
    monkeypatch.setattr(collection, "log_premarket_startup_config", lambda _cfg: _enabled_pm())
    now = datetime(2026, 6, 1, 5, 20, tzinfo=ZoneInfo("America/New_York"))
    event = pm.NewsEvent(
        symbol="AAPL",
        headline="Apple wins AI infrastructure deal",
        source="newsapi",
        score=7.0,
        published_at=now.isoformat(),
    )
    catalyst = NewsCatalyst(
        "AAPL",
        7,
        "Apple wins AI infrastructure deal",
        source="newsapi",
        catalyst_type="ai",
    )
    ranking = pm.PremarketRankEntry("AAPL", 7.0, "ai", "newsapi", 0.9, "ai catalyst")
    monkeypatch.setattr(
        pm,
        "execute_premarket_providers",
        lambda *_args, **_kwargs: pm.PremarketProviderResults(
            newsapi=pm.ProviderExecResult("newsapi", request_sent=True, articles=1, events=[event]),
            alpaca=pm.ProviderExecResult("alpaca", request_sent=False, articles=0),
            sec=pm.ProviderExecResult("sec", request_sent=False, filings=0),
            events=[event],
            catalysts={"AAPL": catalyst},
            rankings=[ranking],
        ),
    )

    rc = collection.main(["--project-root", str(tmp_path), "--force", "--now", "2026-06-01T05:20:00-04:00"])

    out = capsys.readouterr().out
    rankings_payload = json.loads(pm.default_premarket_rankings_path(tmp_path).read_text(encoding="utf-8"))
    state = json.loads(pm.default_state_path(tmp_path).read_text(encoding="utf-8"))
    assert rc == 0
    assert "PREMARKET_COLLECTION_RESULT job=news_5am ran=true persisted=true ranked=1 news=1 filings=0" in out
    assert rankings_payload["rankings"][0]["symbol"] == "AAPL"
    assert rankings_payload["ttl_minutes"] == 390
    assert state["news_5am:2026-06-01"]["status"] == "done"
    assert state["news_5am:2026-06-01"]["ranked"] == 1
