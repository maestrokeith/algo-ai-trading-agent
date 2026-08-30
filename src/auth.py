"""Authentication utilities for AlgoSphere.

Handles:
- Password hashing / verification (bcrypt)
- JWT token creation and verification (PyJWT)

Environment variables
---------------------
JWT_SECRET   — secret key for signing tokens (required in production)
JWT_ALGO     — algorithm, default HS256
JWT_TTL_SECS — token lifetime in seconds, default 86400 (24 h)
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
import jwt

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_JWT_SECRET = os.environ.get("JWT_SECRET", "dev-insecure-secret-change-in-prod")
_JWT_ALGO = os.environ.get("JWT_ALGO", "HS256")
_JWT_TTL = int(os.environ.get("JWT_TTL_SECS", "86400"))


# ---------------------------------------------------------------------------
# Password helpers
# ---------------------------------------------------------------------------

def hash_password(plain: str) -> str:
    """Return a bcrypt hash of *plain*."""
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    """Return True if *plain* matches *hashed*."""
    try:
        return bcrypt.checkpw(plain.encode(), hashed.encode())
    except Exception:
        return False


# ---------------------------------------------------------------------------
# JWT helpers
# ---------------------------------------------------------------------------

def create_token(user_id: str, role: str, extra: dict[str, Any] | None = None) -> str:
    """Create a signed JWT for *user_id* with the given *role*.

    The token encodes ``sub`` (user_id), ``role``, ``iat`` and ``exp``.
    Optional *extra* fields are merged into the payload.
    """
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": user_id,
        "role": role,
        "iat": now,
        "exp": now + timedelta(seconds=_JWT_TTL),
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, _JWT_SECRET, algorithm=_JWT_ALGO)


def decode_token(token: str) -> dict[str, Any]:
    """Decode and verify *token*.

    Raises ``jwt.ExpiredSignatureError`` if expired.
    Raises ``jwt.InvalidTokenError`` for any other invalidity.
    """
    return jwt.decode(token, _JWT_SECRET, algorithms=[_JWT_ALGO])
