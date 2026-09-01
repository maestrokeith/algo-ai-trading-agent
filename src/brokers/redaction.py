"""Recursive redaction for broker responses, diagnostics, and persisted state."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


SENSITIVE_KEYS = {
    "account_id",
    "account_number",
    "broker_account_id",
    "broker_account_number",
    "authorization",
    "access_token",
    "refresh_token",
    "token",
}


def mask_identifier(value: Any) -> str:
    """Return only the final four characters of an identifier."""

    raw = str(value or "")
    suffix = raw[-4:] if raw else ""
    return f"••••{suffix}" if suffix else "••••"


def redact(value: Any) -> Any:
    """Return a recursively redacted copy suitable for logs or persistence."""

    if isinstance(value, Mapping):
        return {
            str(key): mask_identifier(item) if str(key).lower() in SENSITIVE_KEYS else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, tuple):
        return tuple(redact(item) for item in value)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [redact(item) for item in value]
    return value


def redact_text(value: Any, *, secrets: Sequence[str] = ()) -> str:
    """Remove explicitly known sensitive values from arbitrary exception text."""

    text = str(value or "")
    for secret in secrets:
        raw = str(secret or "")
        if raw:
            text = text.replace(raw, mask_identifier(raw))
    return text
