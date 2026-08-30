"""Tests for :mod:`src.alpaca_loop_notify`."""

from __future__ import annotations

import pytest
import pytz

from src import alpaca_loop_notify as mod
from src.alpaca_loop_notify import HeartbeatUserSnapshot
from src.health_monitor import HealthCheckResult


def test_notify_started_calls_send_telegram(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def _capture(msg: str, *, document=None) -> bool:
        calls.append(msg)
        return True

    monkeypatch.setattr(mod, "send_telegram", _capture)
    mod.notify_alpaca_loop_started(mode_label="PAPER", user_ids=["alice", "bob"])
    assert len(calls) == 1
    assert "START" in calls[0]
    assert "PAPER" in calls[0]
    assert "alice" in calls[0] and "bob" in calls[0]
    assert "ET:" in calls[0] and "UTC:" in calls[0]


def test_notify_stopped_includes_reason_and_detail(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def _capture(msg: str, *, document=None) -> bool:
        calls.append(msg)
        return True

    monkeypatch.setattr(mod, "send_telegram", _capture)
    mod.notify_alpaca_loop_stopped(reason="market_closed", detail="End of session.")
    assert len(calls) == 1
    assert "STOP" in calls[0]
    assert "market_closed" in calls[0]
    assert "End of session." in calls[0]


def test_notify_started_empty_users(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    monkeypatch.setattr(mod, "send_telegram", lambda m, document=None: calls.append(m) or True)
    mod.notify_alpaca_loop_started(mode_label="LIVE", user_ids=[])
    assert "(none)" in calls[0]


def test_notify_swallows_send_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(_msg: str, *, document=None) -> bool:
        raise RuntimeError("network down")

    monkeypatch.setattr(mod, "send_telegram", _boom)
    mod.notify_alpaca_loop_started(mode_label="PAPER", user_ids=["u1"])
    mod.notify_alpaca_loop_stopped(reason="x", detail="y")


def test_format_times_naive_utc(monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import datetime, timezone

    naive = datetime(2024, 6, 1, 12, 0, 0)
    et_s, utc_s = mod._format_times(naive)
    assert "UTC" in utc_s
    assert et_s


def test_notify_stopped_truncates_long_detail(monkeypatch: pytest.MonkeyPatch) -> None:
    long_detail = "x" * 2000
    captured: list[str] = []

    def _cap(msg: str, *, document=None) -> bool:
        captured.append(msg)
        return True

    monkeypatch.setattr(mod, "send_telegram", _cap)
    mod.notify_alpaca_loop_stopped(reason="err", detail=long_detail)
    assert "Detail:" in captured[0]
    assert len(captured[0]) < len(long_detail) + 500


def test_heartbeat_interval_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TELEGRAM_HEARTBEAT_INTERVAL_SEC", raising=False)
    assert mod.heartbeat_interval_seconds() == 900.0


def test_heartbeat_interval_disable_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_HEARTBEAT_INTERVAL_SEC", "0")
    assert mod.heartbeat_interval_seconds() is None


def test_heartbeat_interval_disable_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_HEARTBEAT_INTERVAL_SEC", "off")
    assert mod.heartbeat_interval_seconds() is None


def test_heartbeat_interval_custom_and_min(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_HEARTBEAT_INTERVAL_SEC", "180")
    assert mod.heartbeat_interval_seconds() == 180.0
    monkeypatch.setenv("TELEGRAM_HEARTBEAT_INTERVAL_SEC", "30")
    assert mod.heartbeat_interval_seconds() == 60.0


def test_heartbeat_interval_bad_value_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_HEARTBEAT_INTERVAL_SEC", "not-a-number")
    assert mod.heartbeat_interval_seconds() == 900.0


def test_format_heartbeat_single_user() -> None:
    from datetime import datetime

    et = pytz.timezone("America/New_York")
    now = et.localize(datetime(2026, 5, 9, 10, 30, 0))
    row = HeartbeatUserSnapshot(
        user_id="alice",
        mode_label="PAPER",
        equity=100_000.12,
        position_count=3,
        gross_exposure_pct=42.5,
        cash=12_345.67,
        pnl_today_pct=0.35,
    )
    text = mod.format_heartbeat_message([row], now_et=now)
    assert "💓 Algo heartbeat" in text
    assert "Mode: PAPER" in text
    assert "Equity: $100,000.12" in text
    assert "Positions: 3" in text
    assert "Gross Exposure: 42.50%" in text
    assert "Cash: $12,345.67" in text
    assert "PnL Today: +0.35%" in text
    assert "[alice]" not in text


def test_format_heartbeat_multi_user() -> None:
    from datetime import datetime

    et = pytz.timezone("America/New_York")
    now = et.localize(datetime(2026, 5, 9, 14, 0, 0))
    rows = [
        HeartbeatUserSnapshot(
            user_id="alice",
            mode_label="PAPER",
            equity=50_000.0,
            position_count=1,
            gross_exposure_pct=10.0,
            cash=5_000.0,
            pnl_today_pct=None,
        ),
        HeartbeatUserSnapshot(
            user_id="bob",
            mode_label="LIVE",
            equity=200_000.0,
            position_count=0,
            gross_exposure_pct=0.0,
            cash=200_000.0,
            pnl_today_pct=-0.1,
        ),
    ]
    text = mod.format_heartbeat_message(rows, now_et=now)
    assert "[alice]" in text and "[bob]" in text
    assert "PnL Today: n/a" in text
    assert "PnL Today: -0.10%" in text


def test_notify_heartbeat_calls_send_telegram(monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import datetime

    calls: list[str] = []
    monkeypatch.setattr(mod, "send_telegram", lambda m, document=None: calls.append(m) or True)
    et = pytz.timezone("America/New_York")
    now = et.localize(datetime(2026, 5, 9, 9, 35, 0))
    mod.notify_alpaca_loop_heartbeat(
        [
            HeartbeatUserSnapshot(
                user_id="u1",
                mode_label="LIVE",
                equity=1.0,
                position_count=0,
                gross_exposure_pct=0.0,
                cash=1.0,
                pnl_today_pct=0.0,
            )
        ],
        now_et=now,
    )
    assert len(calls) == 1
    assert "💓" in calls[0]


def test_notify_heartbeat_empty_no_send(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(mod, "send_telegram", lambda m, document=None: calls.append(m) or True)
    from datetime import datetime

    et = pytz.timezone("America/New_York")
    mod.notify_alpaca_loop_heartbeat([], now_et=et.localize(datetime(2026, 5, 9, 10, 0, 0)))
    assert calls == []


def test_notify_heartbeat_swallows_send_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import datetime

    def _boom(_m: str, *, document=None) -> bool:
        raise RuntimeError("down")

    monkeypatch.setattr(mod, "send_telegram", _boom)
    et = pytz.timezone("America/New_York")
    mod.notify_alpaca_loop_heartbeat(
        [
            HeartbeatUserSnapshot(
                user_id="u",
                mode_label="PAPER",
                equity=1.0,
                position_count=0,
                gross_exposure_pct=0.0,
                cash=1.0,
                pnl_today_pct=None,
            )
        ],
        now_et=et.localize(datetime(2026, 5, 9, 10, 0, 0)),
    )


def test_format_health_alert_message() -> None:
    from datetime import datetime

    et = pytz.timezone("America/New_York")
    text = mod.format_health_alert_message(
        [("u1", HealthCheckResult("broker", False, "get_clock:RuntimeError"))],
        now_et=et.localize(datetime(2026, 5, 9, 10, 0, 0)),
    )

    assert "HEALTH ALERT" in text
    assert "[u1] broker: get_clock:RuntimeError" in text


def test_notify_health_alert_calls_send_telegram(monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import datetime

    calls: list[str] = []
    monkeypatch.setattr(mod, "send_telegram", lambda m, document=None: calls.append(m) or True)
    et = pytz.timezone("America/New_York")

    mod.notify_alpaca_loop_health_alert(
        [("u1", HealthCheckResult("news", False, "pipeline_missing"))],
        now_et=et.localize(datetime(2026, 5, 9, 10, 0, 0)),
    )

    assert len(calls) == 1
    assert "news: pipeline_missing" in calls[0]
