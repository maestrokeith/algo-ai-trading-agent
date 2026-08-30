from __future__ import annotations

from types import SimpleNamespace

import scripts.show_open_orders as show_open_orders


def test_format_open_orders_includes_option_contract_symbol() -> None:
    orders = [
        SimpleNamespace(
            symbol="AAPL260619C00200000",
            side="buy",
            qty="1",
            status="accepted",
            submitted_at="2026-06-09T14:30:00Z",
        )
    ]

    text = show_open_orders.format_open_orders(orders)

    assert "symbol\tside\tqty\tstatus\tsubmitted_at" in text
    assert "AAPL260619C00200000\tbuy\t1\taccepted\t2026-06-09T14:30:00Z" in text


def test_main_uses_paper_broker_and_prints_open_orders(monkeypatch, capsys) -> None:
    constructed: list[dict] = []

    class FakeBroker:
        def __init__(self, *, api_key: str | None, secret: str | None, paper: bool) -> None:
            constructed.append({"api_key": api_key, "secret": secret, "paper": paper})

        def list_orders(self, status: str = "open"):
            assert status == "open"
            return [
                {
                    "symbol": "SPY260619P00500000",
                    "side": "sell",
                    "qty": "2",
                    "status": "new",
                    "submitted_at": "2026-06-09T14:31:00Z",
                }
            ]

    monkeypatch.setattr(show_open_orders, "AlpacaBroker", FakeBroker)
    monkeypatch.setenv("APCA_API_KEY_ID", "paper-key")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "paper-secret")

    assert show_open_orders.main(["--mode", "paper"]) == 0

    assert constructed == [{"api_key": "paper-key", "secret": "paper-secret", "paper": True}]
    out = capsys.readouterr().out
    assert "open_orders_context mode=paper user=paper_bot base_url_type=paper_trading" in out
    assert "SPY260619P00500000\tsell\t2\tnew\t2026-06-09T14:31:00Z" in out


def test_main_live_flag_uses_live_broker(monkeypatch, capsys) -> None:
    constructed: list[dict] = []

    class FakeBroker:
        def __init__(self, *, api_key: str | None, secret: str | None, paper: bool) -> None:
            constructed.append({"api_key": api_key, "secret": secret, "paper": paper})

        def list_orders(self, status: str = "open"):
            return []

    monkeypatch.setattr(show_open_orders, "AlpacaBroker", FakeBroker)
    monkeypatch.setenv("ALPACA_LIVE_API_KEY_ID", "live-key")
    monkeypatch.setenv("ALPACA_LIVE_API_SECRET_KEY", "live-secret")

    assert show_open_orders.main(["--mode", "live"]) == 0

    assert constructed == [{"api_key": "live-key", "secret": "live-secret", "paper": False}]
    out = capsys.readouterr().out
    assert "open_orders_context mode=live user=live_bot base_url_type=live_trading" in out
    assert out.endswith("symbol\tside\tqty\tstatus\tsubmitted_at\n")


def test_main_reports_missing_live_credentials_without_traceback(monkeypatch, capsys) -> None:
    class FakeBroker:
        def __init__(self, *args, **kwargs) -> None:
            raise AssertionError("broker should not be constructed without credentials")

    monkeypatch.setattr(show_open_orders, "AlpacaBroker", FakeBroker)
    monkeypatch.delenv("ALPACA_LIVE_API_KEY_ID", raising=False)
    monkeypatch.delenv("ALPACA_LIVE_API_SECRET_KEY", raising=False)

    assert show_open_orders.main(["--mode", "live"]) == 0

    captured = capsys.readouterr()
    assert "open_orders_context mode=live user=live_bot base_url_type=live_trading" in captured.out
    assert captured.out.endswith("symbol\tside\tqty\tstatus\tsubmitted_at\n")
    assert (
        "open_orders_unavailable: missing credentials for live mode: "
        "ALPACA_LIVE_API_KEY_ID, ALPACA_LIVE_API_SECRET_KEY"
    ) in captured.err


def test_main_reports_missing_paper_credentials_without_traceback(monkeypatch, capsys) -> None:
    class FakeBroker:
        def __init__(self, *args, **kwargs) -> None:
            raise AssertionError("broker should not be constructed without credentials")

    monkeypatch.setattr(show_open_orders, "AlpacaBroker", FakeBroker)
    monkeypatch.delenv("APCA_API_KEY_ID", raising=False)
    monkeypatch.delenv("APCA_API_SECRET_KEY", raising=False)

    assert show_open_orders.main(["--mode", "paper", "--user", "paper_bot"]) == 0

    captured = capsys.readouterr()
    assert "open_orders_context mode=paper user=paper_bot base_url_type=paper_trading" in captured.out
    assert captured.out.endswith("symbol\tside\tqty\tstatus\tsubmitted_at\n")
    assert (
        "open_orders_unavailable: missing credentials for paper mode: "
        "APCA_API_KEY_ID, APCA_API_SECRET_KEY"
    ) in captured.err
