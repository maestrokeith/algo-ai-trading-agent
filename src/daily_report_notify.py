"""Deliver end-of-day HTML reports via Telegram and/or SMTP (credentials from environment only).

Telegram (Bot API):

- ``TELEGRAM_BOT_TOKEN`` — bot token from @BotFather
- ``TELEGRAM_CHAT_ID`` — destination chat or channel id
- ``TELEGRAM_SEND_HTML=0`` — set to skip attaching the HTML file (text summary only)

SMTP:

- ``SMTP_HOST``, ``SMTP_PORT`` (default ``587``)
- ``SMTP_USER``, ``SMTP_PASSWORD`` — auth (omit user/password for open relay, not recommended)
- ``EMAIL_FROM`` — From header
- ``EMAIL_TO`` — comma-separated recipients
- ``SMTP_USE_TLS`` — default ``1`` (STARTTLS on 587). Use ``0`` with ``SMTP_PORT=465`` and set ``SMTP_SSL=1``.

All sends are best-effort: failures are logged and never propagate.
"""
from __future__ import annotations

import json
import logging
import mimetypes
import os
import smtplib
import ssl
import urllib.error
import urllib.request
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Mapping

log = logging.getLogger(__name__)


def send_telegram(message: str, *, document: Path | None = None) -> bool:
    """
    Post *message* to Telegram (and optionally upload *document*).

    Example::

        send_telegram("Daily PnL: +$364")

    Returns True if at least one Telegram request succeeded (or nothing to do).
    """
    chat_id = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    if not chat_id:
        log.debug("Telegram skipped: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID unset")
        return True
    return send_telegram_to_chat(chat_id, message, document=document)


def send_telegram_to_chat(
    chat_id: str,
    message: str,
    *,
    document: Path | None = None,
) -> bool:
    """Post *message* to a specific Telegram *chat_id* using the configured bot token."""
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat_id = str(chat_id or "").strip()
    if not token or not chat_id:
        log.debug("Telegram skipped: TELEGRAM_BOT_TOKEN or chat_id unset")
        return True

    send_html = os.environ.get("TELEGRAM_SEND_HTML", "1").strip().lower() not in ("0", "false", "no")
    caption = message[:1024] if len(message) > 1024 else message

    ok = True
    if not _telegram_send_message(token, chat_id, message):
        ok = False
    if send_html and document is not None and document.is_file():
        if not _telegram_send_document(token, chat_id, document, caption=caption):
            ok = False
    return ok


def _telegram_api_post(token: str, method: str, fields: dict[str, Any]) -> bool:
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = json.dumps(fields).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        log.warning("Telegram HTTP %s: %s", e.code, body[:500])
        return False
    except OSError as e:
        log.warning("Telegram request failed: %s", e)
        return False
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("Telegram invalid JSON response")
        return False
    if not payload.get("ok"):
        log.warning("Telegram API error: %s", payload.get("description", payload))
        return False
    return True


def _telegram_send_message(token: str, chat_id: str, text: str) -> bool:
    return _telegram_api_post(
        token,
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        },
    )


def _telegram_send_document(token: str, chat_id: str, path: Path, *, caption: str) -> bool:
    boundary = os.urandom(16).hex()
    crlf = b"\r\n"
    parts: list[bytes] = []

    def add_field(name: str, value: str) -> None:
        parts.append(f"--{boundary}".encode())
        parts.append(f'Content-Disposition: form-data; name="{name}"'.encode())
        parts.append(b"")
        parts.append(value.encode("utf-8"))

    add_field("chat_id", chat_id)
    add_field("caption", caption[:1024])
    parts.append(f"--{boundary}".encode())
    parts.append(
        f'Content-Disposition: form-data; name="document"; filename="{path.name}"'.encode()
    )
    ctype, _ = mimetypes.guess_type(path.name)
    parts.append(f"Content-Type: {ctype or 'application/octet-stream'}".encode())
    parts.append(b"")
    parts.append(path.read_bytes())
    parts.append(f"--{boundary}--".encode())
    body = crlf.join(parts)

    url = f"https://api.telegram.org/bot{token}/sendDocument"
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        log.warning("Telegram sendDocument HTTP %s", e.code)
        return False
    except OSError as e:
        log.warning("Telegram sendDocument failed: %s", e)
        return False
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return False
    if not payload.get("ok"):
        log.warning("Telegram sendDocument: %s", payload.get("description"))
        return False
    return True


def send_smtp_email(
    subject: str,
    body_plain: str,
    *,
    html_attachment: Path | None = None,
) -> bool:
    """Send one email when SMTP env is configured; optional HTML file attachment."""
    host = (os.environ.get("SMTP_HOST") or "").strip()
    if not host:
        log.debug("SMTP skipped: SMTP_HOST unset")
        return True

    port = int(os.environ.get("SMTP_PORT") or "587")
    user = (os.environ.get("SMTP_USER") or "").strip()
    password = (os.environ.get("SMTP_PASSWORD") or "").strip()
    mail_from = (os.environ.get("EMAIL_FROM") or user or "").strip()
    to_raw = (os.environ.get("EMAIL_TO") or "").strip()
    if not mail_from or not to_raw:
        log.warning("SMTP skipped: EMAIL_FROM or EMAIL_TO unset")
        return False

    recipients = [x.strip() for x in to_raw.split(",") if x.strip()]
    use_tls = os.environ.get("SMTP_USE_TLS", "1").strip().lower() not in ("0", "false", "no")
    use_ssl = os.environ.get("SMTP_SSL", "0").strip().lower() in ("1", "true", "yes")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = mail_from
    msg["To"] = ", ".join(recipients)
    msg.set_content(body_plain)
    if html_attachment is not None and html_attachment.is_file():
        data = html_attachment.read_bytes()
        msg.add_attachment(
            data,
            maintype="text",
            subtype="html",
            filename=html_attachment.name,
        )

    try:
        if use_ssl:
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(host, port, context=context, timeout=60) as smtp:
                if user:
                    smtp.login(user, password)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=60) as smtp:
                if use_tls:
                    context = ssl.create_default_context()
                    smtp.starttls(context=context)
                if user:
                    smtp.login(user, password)
                smtp.send_message(msg)
    except OSError as e:
        log.warning("SMTP send failed: %s", e)
        return False
    return True


def _format_pnl_line(pnl: float) -> str:
    """Human-readable line like ``Daily PnL: +$364.00`` / ``Daily PnL: -$10.50``."""
    ap = abs(float(pnl))
    sign = "+" if float(pnl) >= 0 else "-"
    return f"Daily PnL: {sign}${ap:,.2f}"


def deliver_daily_report(
    *,
    html_path: Path,
    account: Mapping[str, Any],
    exposure: Mapping[str, Any],
    user_label: str,
) -> None:
    """Send summary (+ optional HTML) via Telegram and/or SMTP based on env."""
    equity = float(account.get("equity") or 0.0)
    pnl = float(account.get("pnl_today") or 0.0)
    total_contributed = 0.0
    for key in (
        "total_contributed_usd",
        "total_contributed",
        "capital_contributed_usd",
        "capital_contributed",
    ):
        raw = account.get(key)
        try:
            total_contributed = float(raw)
        except (TypeError, ValueError):
            continue
        if total_contributed > 0:
            break
        total_contributed = 0.0
    gross = float(exposure.get("gross") or 0.0)
    net = float(exposure.get("net") or 0.0)

    summary_lines = [
        _format_pnl_line(pnl),
        f"Equity: ${equity:,.2f}",
    ]
    if total_contributed > 0:
        lifetime_profit = equity - total_contributed
        total_return_pct = (lifetime_profit / total_contributed) * 100.0
        summary_lines.extend(
            [
                f"Contributed capital: ${total_contributed:,.2f}",
                f"Lifetime profit: ${lifetime_profit:,.2f}",
                f"Total return: {total_return_pct:,.2f}%",
            ]
        )
    summary_lines.extend(
        [
        f"Gross / net exposure: {gross:.1f}% / {net:.1f}%",
        f"User: {user_label}",
        f"Report: {html_path}",
        ]
    )
    summary = "\n".join(summary_lines)
    subject = f"AlgoSphere daily — {user_label} — {_format_pnl_line(pnl)}"

    try:
        send_telegram(summary, document=html_path)
    except Exception as e:
        log.warning("[%s] Telegram notify failed: %s", user_label, e)

    try:
        send_smtp_email(subject, summary, html_attachment=html_path)
    except Exception as e:
        log.warning("[%s] SMTP notify failed: %s", user_label, e)
