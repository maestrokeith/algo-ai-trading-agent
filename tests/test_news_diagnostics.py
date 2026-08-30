from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import scripts.news_diagnostics as cli
import src.news_diagnostics as nd
from src.premarket_intelligence import NewsEvent, ProviderExecResult

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_news_diagnostics_alpaca_payload_and_artifact(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(nd, "_credentials_present", lambda provider, cfg: provider == "alpaca")

    def _fake_alpaca(symbols, cfg, timeout_seconds, *, now=None, market_client=None):
        assert symbols == ["AAPL"]
        assert cfg["news_sentiment"]["headline_lookback_hours"] == 12
        assert cfg["news_sentiment"]["max_headlines"] == 7
        return ProviderExecResult(
            provider="alpaca",
            request_sent=True,
            http_status=200,
            raw_articles_before_filter=2,
            articles_after_filter=1,
            articles=1,
            sample_article_titles=["Raw Apple title", "Other raw title"],
            events=[NewsEvent(symbol="AAPL", headline="Filtered Apple title", source="alpaca")],
        )

    monkeypatch.setattr(nd, "fetch_alpaca_news_events", _fake_alpaca)

    payload = nd.run_news_diagnostic(
        provider="alpaca",
        symbol="aapl",
        hours=12,
        limit=7,
        config={},
        project_root=tmp_path,
    )

    artifact = tmp_path / "data" / "premarket" / "news_diagnostics_latest.json"
    saved = json.loads(artifact.read_text(encoding="utf-8"))
    assert payload["provider"] == "alpaca"
    assert payload["symbol"] == "AAPL"
    assert payload["credentials_present"] is True
    assert payload["request_sent"] is True
    assert payload["http_status"] == 200
    assert payload["raw_count"] == 2
    assert payload["filtered_count"] == 1
    assert payload["raw_headlines"] == ["Raw Apple title", "Other raw title"]
    assert payload["filtered_headlines"] == ["Filtered Apple title"]
    assert saved["provider"] == "alpaca"


def test_news_diagnostics_newsapi_empty_reason(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(nd, "_credentials_present", lambda provider, cfg: True)
    monkeypatch.setattr(
        nd,
        "fetch_newsapi_articles",
        lambda *a, **k: ProviderExecResult(
            provider="newsapi",
            request_sent=True,
            http_status=200,
            raw_articles_before_filter=0,
            articles_after_filter=0,
            articles=0,
        ),
    )

    payload = nd.run_news_diagnostic(
        provider="newsapi",
        symbol="MSFT",
        config={},
        project_root=tmp_path,
    )

    assert payload["reason"] == "no_raw_results"
    assert "reason=no_raw_results" in nd.format_news_diagnostic(payload)


def test_news_diagnostics_newsapi_disabled_reason(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(nd, "_credentials_present", lambda provider, cfg: provider == "newsapi")

    payload = nd.run_news_diagnostic(
        provider="newsapi",
        symbol="MSFT",
        config={
            "premarket_intelligence": {
                "newsapi": {"enabled": False},
                "newsapi_enabled": True,
            },
            "news_sentiment": {"enabled": True},
        },
        project_root=tmp_path,
    )

    assert payload["credentials_present"] is True
    assert payload["request_sent"] is False
    assert payload["reason"] == "newsapi_disabled"
    assert "reason=newsapi_disabled" in nd.format_news_diagnostic(payload)


def test_news_diagnostics_sec_uses_filing_count(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(nd, "_credentials_present", lambda provider, cfg: True)
    monkeypatch.setattr(
        nd,
        "fetch_sec_filings",
        lambda *a, **k: ProviderExecResult(
            provider="sec",
            request_sent=True,
            http_status=200,
            filings=3,
            events=[NewsEvent(symbol="AAPL", headline="AAPL 8-K filed", source="sec")],
        ),
    )

    payload = nd.run_news_diagnostic(provider="sec", symbol="AAPL", config={}, project_root=tmp_path)

    assert payload["provider"] == "sec"
    assert payload["credentials_present"] is True
    assert payload["raw_count"] == 3
    assert payload["filtered_count"] == 3
    assert payload["filtered_headlines"] == ["AAPL 8-K filed"]


def test_news_diagnostics_cli_writes_artifact(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys) -> None:
    monkeypatch.setattr(cli, "load_app_config", lambda path: {})
    monkeypatch.setattr(
        cli,
        "run_news_diagnostic",
        lambda **kwargs: {
            "provider": kwargs["provider"],
            "symbol": kwargs["symbol"].upper(),
            "request_parameters": {
                "symbol": kwargs["symbol"].upper(),
                "hours": kwargs["hours"],
                "limit": kwargs["limit"],
            },
            "credentials_present": False,
            "request_sent": False,
            "http_status": None,
            "raw_count": 0,
            "filtered_count": 0,
            "raw_headlines": [],
            "filtered_headlines": [],
            "reason": "missing_api_key",
            "artifact_path": str(tmp_path / "data" / "premarket" / "news_diagnostics_latest.json"),
        },
    )

    rc = cli.main(["--provider", "alpaca", "--symbol", "AAPL", "--hours", "24", "--limit", "10", "--project-root", str(tmp_path)])

    out = capsys.readouterr().out
    assert rc == 0
    assert "NEWS_DIAGNOSTICS provider=alpaca symbol=AAPL" in out
    assert "reason=missing_api_key" in out


def test_news_diagnostics_direct_script_help() -> None:
    proc = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "news_diagnostics.py"), "--help"],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert proc.returncode == 0
    assert "--provider" in proc.stdout
    assert "--symbol" in proc.stdout
