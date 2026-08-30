"""Tests for :mod:`src.daily_report_notify`."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from src.daily_report_notify import (
    _format_pnl_line,
    deliver_daily_report,
    send_smtp_email,
    send_telegram,
)


def test_format_pnl_line() -> None:
    pos = _format_pnl_line(364.0)
    assert pos.startswith("Daily PnL:")
    assert "+$" in pos and "364" in pos
    neg = _format_pnl_line(-10.5)
    assert "Daily PnL:" in neg
    assert "-$" in neg and "10" in neg


def test_send_telegram_skips_without_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert send_telegram("Daily PnL: +$0") is True


def test_send_telegram_message_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")

    def fake_urlopen(req: object, timeout: float | None = None) -> object:
        class Resp:
            def read(self) -> bytes:
                return b'{"ok":true,"result":{}}'

            def __enter__(self) -> object:
                return self

            def __exit__(self, *a: object) -> None:
                return None

        return Resp()

    with patch("urllib.request.urlopen", fake_urlopen):
        assert send_telegram("Daily PnL: +$364", document=None) is True


def test_send_telegram_api_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")

    def fake_urlopen(req: object, timeout: float | None = None) -> object:
        class Resp:
            def read(self) -> bytes:
                return b'{"ok":false,"description":"bad"}'

            def __enter__(self) -> object:
                return self

            def __exit__(self, *a: object) -> None:
                return None

        return Resp()

    with patch("urllib.request.urlopen", fake_urlopen):
        assert send_telegram("x") is False


def test_send_telegram_with_document(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "99")
    f = tmp_path / "r.html"
    f.write_text("<html></html>", encoding="utf-8")

    responses = [
        b'{"ok":true,"result":{}}',
        b'{"ok":true,"result":{}}',
    ]

    def fake_urlopen(req: object, timeout: float | None = None) -> object:
        class Resp:
            def read(self) -> bytes:
                return responses.pop(0)

            def __enter__(self) -> object:
                return self

            def __exit__(self, *a: object) -> None:
                return None

        return Resp()

    with patch("urllib.request.urlopen", fake_urlopen):
        assert send_telegram("Summary line", document=f) is True


def test_send_smtp_skips_without_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SMTP_HOST", raising=False)
    assert send_smtp_email("s", "b") is True


def test_send_smtp_sends(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SMTP_HOST", "localhost")
    monkeypatch.setenv("SMTP_PORT", "25")
    monkeypatch.setenv("SMTP_USE_TLS", "0")
    monkeypatch.setenv("SMTP_SSL", "0")
    monkeypatch.setenv("EMAIL_FROM", "a@example.com")
    monkeypatch.setenv("EMAIL_TO", "b@example.com")
    html = tmp_path / "d.html"
    html.write_text("<p>x</p>", encoding="utf-8")

    sent: list[object] = []

    class FakeSMTP:
        def __init__(self, *a: object, **k: object) -> None:
            pass

        def __enter__(self) -> FakeSMTP:
            return self

        def __exit__(self, *a: object) -> None:
            return None

        def send_message(self, msg: object) -> None:
            sent.append(msg)

    with patch("smtplib.SMTP", FakeSMTP):
        assert send_smtp_email("subj", "body", html_attachment=html) is True
    assert len(sent) == 1


def test_deliver_daily_report_swallows_inner_errors(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")

    def boom(*a: object, **k: object) -> object:
        raise OSError("net")

    p = tmp_path / "out.html"
    p.write_text("h", encoding="utf-8")
    with patch("urllib.request.urlopen", boom):
        deliver_daily_report(
            html_path=p,
            account={"equity": 1.0, "pnl_today": 2.0},
            exposure={"gross": 1.0, "net": 1.0},
            user_label="u1",
        )
