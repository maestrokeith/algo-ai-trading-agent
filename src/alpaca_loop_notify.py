"""Telegram lifecycle alerts for ``scripts/run_alpaca_loop.py`` (start / stop / reason).

Uses the same env credentials as daily reports: ``TELEGRAM_BOT_TOKEN``, ``TELEGRAM_CHAT_ID``.
All sends are best-effort (log on failure, never raise to the caller).

During regular session, optional periodic heartbeats (default every 15 minutes) with
equity / exposure / cash / session PnL. Configure with ``TELEGRAM_HEARTBEAT_INTERVAL_SEC``
(default ``900``); set to ``0`` to disable.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Sequence

import pytz

from src.daily_report_notify import send_telegram
from src.health_monitor import HealthCheckResult

log = logging.getLogger(__name__)

_ET = pytz.timezone("America/New_York")


def heartbeat_interval_seconds() -> float | None:
    """Wall-clock cadence between Telegram heartbeats while the loop is in session.

    Returns ``None`` when heartbeats are disabled (``TELEGRAM_HEARTBEAT_INTERVAL_SEC`` ≤ 0
    or set to ``off`` / ``false`` / ``no``). Minimum 60 seconds when enabled.
    Unset env → 900 (15 minutes).
    """
    env_val = os.environ.get("TELEGRAM_HEARTBEAT_INTERVAL_SEC")
    if env_val is None or not str(env_val).strip():
        return 900.0
    raw = str(env_val).strip()
    if raw.lower() in ("off", "false", "no"):
        return None
    try:
        sec = float(raw)
    except ValueError:
        return 900.0
    if sec <= 0:
        return None
    return max(60.0, sec)


@dataclass(frozen=True)
class HeartbeatUserSnapshot:
    """One row of portfolio summary for a Telegram heartbeat (single user)."""

    user_id: str
    mode_label: str
    equity: float
    position_count: int
    gross_exposure_pct: float
    cash: float
    pnl_today_pct: float | None


def format_heartbeat_message(
    rows: Sequence[HeartbeatUserSnapshot],
    *,
    now_et: datetime,
) -> str:
    """Build the human-readable heartbeat body (no Telegram I/O)."""
    et_s = now_et.astimezone(_ET).strftime("%Y-%m-%d %H:%M:%S %Z")
    lines: list[str] = ["💓 Algo heartbeat", f"ET: {et_s}", ""]
    multi = len(rows) > 1
    for i, row in enumerate(rows):
        if multi:
            lines.append(f"[{row.user_id}]")
        lines.extend(
            [
                f"Mode: {row.mode_label}",
                f"Equity: ${_fmt_money(row.equity)}",
                f"Positions: {row.position_count}",
                f"Gross Exposure: {_fmt_pct_plain(row.gross_exposure_pct)}%",
                f"Cash: ${_fmt_money(row.cash)}",
                f"PnL Today: {_fmt_signed_pct(row.pnl_today_pct)}",
            ]
        )
        if i < len(rows) - 1:
            lines.append("")
    return "\n".join(lines)


def _fmt_money(x: float) -> str:
    return f"{x:,.2f}"


def _fmt_pct_plain(x: float) -> str:
    return f"{x:.2f}"


def _fmt_signed_pct(x: float | None) -> str:
    if x is None:
        return "n/a"
    sign = "+" if x > 0 else ""
    return f"{sign}{x:.2f}%"


def notify_alpaca_loop_heartbeat(
    rows: Sequence[HeartbeatUserSnapshot],
    *,
    now_et: datetime,
) -> None:
    """Post a Telegram heartbeat when *rows* is non-empty (same credentials as start/stop)."""
    if not rows:
        return
    try:
        send_telegram(format_heartbeat_message(rows, now_et=now_et))
    except Exception as exc:
        log.warning("Alpaca loop heartbeat Telegram notify failed: %s", exc)


def format_health_alert_message(
    failures: Sequence[tuple[str, HealthCheckResult]],
    *,
    now_et: datetime,
) -> str:
    """Build a concise health alert body."""
    et_s = now_et.astimezone(_ET).strftime("%Y-%m-%d %H:%M:%S %Z")
    lines = ["AlgoSphere HEALTH ALERT", f"ET: {et_s}"]
    for user_id, result in failures:
        lines.append(f"[{user_id}] {result.name}: {result.reason}")
    return "\n".join(lines)


def notify_alpaca_loop_health_alert(
    failures: Sequence[tuple[str, HealthCheckResult]],
    *,
    now_et: datetime,
) -> None:
    """Post a Telegram health alert for failed runtime checks."""
    if not failures:
        return
    try:
        send_telegram(format_health_alert_message(failures, now_et=now_et))
    except Exception as exc:
        log.warning("Alpaca loop health Telegram notify failed: %s", exc)


def _format_times(now_utc: datetime | None = None) -> tuple[str, str]:
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    elif now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    et = now_utc.astimezone(_ET)
    utc_s = now_utc.strftime("%Y-%m-%d %H:%M:%S UTC")
    et_s = et.strftime("%Y-%m-%d %H:%M:%S %Z")
    return et_s, utc_s


def notify_alpaca_loop_started(*, mode_label: str, user_ids: Sequence[str]) -> None:
    """Post a Telegram message when the main trading loop begins (after init and locks)."""
    try:
        et_s, utc_s = _format_times()
        users = ", ".join(str(u) for u in user_ids) if user_ids else "(none)"
        body = (
            "AlgoSphere — Alpaca loop START\n"
            f"ET: {et_s}\n"
            f"UTC: {utc_s}\n"
            f"Mode: {mode_label}\n"
            f"Users: {users}"
        )
        send_telegram(body)
    except Exception as exc:
        log.warning("Alpaca loop start Telegram notify failed: %s", exc)


def notify_alpaca_loop_stopped(*, reason: str, detail: str = "") -> None:
    """Post a Telegram message when the loop ends (market close, risk stop, Ctrl+C, crash, etc.)."""
    try:
        et_s, utc_s = _format_times()
        lines = [
            "AlgoSphere — Alpaca loop STOP",
            f"ET: {et_s}",
            f"UTC: {utc_s}",
            f"Reason: {reason}",
        ]
        d = (detail or "").strip()
        if d:
            lines.append(f"Detail: {d[:800]}")
        send_telegram("\n".join(lines))
    except Exception as exc:
        log.warning("Alpaca loop stop Telegram notify failed: %s", exc)
