"""Tests for Alpaca credential resolution helpers."""

from __future__ import annotations

import pytest

from src.brokers.alpaca_client import resolve_alpaca_credentials, resolve_alpaca_paper_flag


def test_resolve_alpaca_paper_flag_from_alpaca_live_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALPACA_LIVE", "true")
    assert resolve_alpaca_paper_flag({"broker": {"paper": True}}) is False


def test_resolve_alpaca_credentials_prefers_broker_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALPACA_LIVE_API_KEY_ID", "live-key")
    monkeypatch.setenv("ALPACA_LIVE_API_SECRET_KEY", "live-secret")
    creds = resolve_alpaca_credentials(
        {"broker": {"paper": False, "api_key": "cfg-key", "secret_key": "cfg-secret"}},
        paper=False,
    )
    assert creds.selected == "config"
    assert creds.api_key == "cfg-key"
