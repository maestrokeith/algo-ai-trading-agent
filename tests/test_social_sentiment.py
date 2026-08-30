from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

import scripts.social_sentiment as cli
import src.social_sentiment as ss

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class FakeResponse:
    def __init__(self, status_code: int, payload: dict) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        return self._payload


def _enabled_config() -> dict:
    return {
        "premarket_intelligence": {
            "social": {
                "enabled": True,
                "reddit": {"enabled": True},
                "twitter": {"enabled": False},
                "subreddits": ["stocks"],
                "min_unique_authors": 2,
                "max_mentions_per_author": 1,
            }
        }
    }


def test_social_sentiment_missing_reddit_credentials_skips_cleanly(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("REDDIT_CLIENT_ID", raising=False)
    monkeypatch.delenv("REDDIT_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("REDDIT_USER_AGENT", raising=False)

    payload = ss.collect_social_sentiment(
        symbols=["AAPL", "NVDA"],
        config=_enabled_config(),
        project_root=tmp_path,
        now=datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc),
    )

    artifact = tmp_path / "data" / "premarket" / "social_sentiment_latest.json"
    saved = json.loads(artifact.read_text(encoding="utf-8"))
    assert payload["providers"]["reddit"]["enabled"] is True
    assert payload["providers"]["reddit"]["request_sent"] is False
    assert payload["providers"]["reddit"]["reason"] == "reddit_credentials_missing"
    assert payload["providers"]["twitter"]["enabled"] is False
    assert payload["providers"]["twitter"]["reason"] == "twitter_disabled"
    assert saved["providers"]["reddit"]["reason"] == "reddit_credentials_missing"


def test_fetch_reddit_mentions_uses_oauth_and_filters_posts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REDDIT_CLIENT_ID", "client")
    monkeypatch.setenv("REDDIT_CLIENT_SECRET", "secret")
    monkeypatch.setenv("REDDIT_USER_AGENT", "algosphere-test/1.0")
    calls: list[tuple[str, dict]] = []
    now = datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)

    def fake_post(url, **kwargs):
        calls.append(("post", {"url": url, **kwargs}))
        return FakeResponse(200, {"access_token": "token"})

    def fake_get(url, **kwargs):
        calls.append(("get", {"url": url, **kwargs}))
        return FakeResponse(
            200,
            {
                "data": {
                    "children": [
                        {
                            "data": {
                                "title": "AAPL bullish breakout after upgrade",
                                "author": "author1",
                                "created_utc": now.timestamp(),
                                "author_created_utc": (now.replace(year=2025)).timestamp(),
                                "score": 30,
                            }
                        },
                        {
                            "data": {
                                "title": "AAPL upgrade has upside",
                                "author": "author2",
                                "created_utc": now.timestamp(),
                                "author_created_utc": (now.replace(year=2024)).timestamp(),
                                "score": 25,
                            }
                        },
                        {
                            "data": {
                                "title": "AAPL to the moon guaranteed",
                                "author": "pump1",
                                "created_utc": now.timestamp(),
                                "author_created_utc": (now.replace(year=2023)).timestamp(),
                                "score": 50,
                            }
                        },
                        {
                            "data": {
                                "title": "AAPL bullish from brand new account",
                                "author": "newbie",
                                "created_utc": now.timestamp(),
                                "author_created_utc": now.timestamp(),
                                "score": 40,
                            }
                        },
                    ]
                }
            },
        )

    monkeypatch.setattr(ss.requests, "post", fake_post)
    monkeypatch.setattr(ss.requests, "get", fake_get)

    diag, posts = ss.fetch_reddit_mentions(
        ["AAPL"],
        config=_enabled_config(),
        now=now,
        hours=24,
        limit=10,
    )

    assert calls[0][0] == "post"
    assert calls[1][0] == "get"
    assert "oauth.reddit.com" in calls[1][1]["url"]
    assert diag.request_sent is True
    assert diag.http_status == 200
    assert diag.raw_count == 4
    assert diag.filtered_count == 2
    assert [post.author for post in posts] == ["author1", "author2"]


def test_aggregate_social_posts_caps_author_and_requires_unique_authors() -> None:
    posts = [
        ss.SocialPost("PLTR", "PLTR bullish upgrade", "same", "reddit:stocks", 1, score=10),
        ss.SocialPost("PLTR", "PLTR bullish upside", "same", "reddit:stocks", 2, score=9),
        ss.SocialPost("PLTR", "PLTR bearish warning", "other", "reddit:stocks", 3, score=8),
    ]

    rows = ss.aggregate_social_posts(
        ["PLTR"],
        posts,
        hours=24,
        config={
            "premarket_intelligence": {
                "social": {
                    "enabled": True,
                    "max_mentions_per_author": 1,
                    "min_unique_authors": 2,
                }
            }
        },
    )

    row = rows["PLTR"]
    assert row.mention_count == 2
    assert row.unique_author_count == 2
    assert row.bullish_count == 1
    assert row.bearish_count == 1
    assert row.passed_min_unique_authors is True
    assert row.sentiment_score == 0.0


def test_social_sentiment_cli_writes_artifact(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys) -> None:
    monkeypatch.setattr(cli, "load_app_config", lambda path: _enabled_config())
    monkeypatch.setattr(
        cli,
        "collect_social_sentiment",
        lambda **kwargs: {
            "symbols": kwargs["symbols"],
            "hours": kwargs["hours"],
            "artifact_path": str(tmp_path / "data" / "premarket" / "social_sentiment_latest.json"),
            "providers": {
                "reddit": {
                    "enabled": True,
                    "request_sent": False,
                    "http_status": None,
                    "raw_count": 0,
                    "filtered_count": 0,
                    "rate_limited": False,
                    "reason": "reddit_credentials_missing",
                },
                "twitter": {
                    "enabled": False,
                    "request_sent": False,
                    "http_status": None,
                    "raw_count": 0,
                    "filtered_count": 0,
                    "rate_limited": False,
                    "reason": "twitter_disabled",
                },
            },
            "items": [],
        },
    )

    rc = cli.main(["--symbols", "AAPL,NVDA,PLTR", "--hours", "24", "--project-root", str(tmp_path)])

    out = capsys.readouterr().out
    assert rc == 0
    assert "SOCIAL_SENTIMENT symbols=AAPL,NVDA,PLTR hours=24" in out
    assert "SOCIAL_PROVIDER provider=reddit" in out


def test_social_sentiment_direct_script_help() -> None:
    proc = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "social_sentiment.py"), "--help"],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert proc.returncode == 0
    assert "--symbols" in proc.stdout
