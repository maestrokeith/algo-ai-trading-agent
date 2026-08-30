"""Resolve SQLAlchemy database URL from environment (shared by runtime and Alembic)."""
from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


def resolve_database_dsn() -> str:
    """
    Connection string order:

    1. ``DATABASE_URL`` — generic (Postgres, MySQL/TiDB, SQLite file URL, etc.)
    2. ``TIDB_DSN`` — MySQL-compatible (TiDB); optional ``TIDB_SSL_CA`` appended when missing from DSN
    3. ``ALGOSPHERE_LOCAL_SQLITE=1`` (or ``true``) — file ``data/algosphere.db`` under repo root (gitignored)
    4. Fallback: in-memory SQLite (tests / ephemeral dev only)
    """
    for key in ("DATABASE_URL", "TIDB_DSN"):
        raw = os.environ.get(key, "")
        if raw and str(raw).strip():
            dsn = str(raw).strip()
            ssl_ca = os.environ.get("TIDB_SSL_CA", "")
            if ssl_ca and "ssl_ca" not in dsn:
                sep = "&" if "?" in dsn else "?"
                dsn = f"{dsn}{sep}ssl_ca={ssl_ca}"
            return dsn

    local = os.environ.get("ALGOSPHERE_LOCAL_SQLITE", "").strip().lower()
    if local in ("1", "true", "yes", "on"):
        root = Path(__file__).resolve().parents[2]
        dbpath = root / "data" / "algosphere.db"
        dbpath.parent.mkdir(parents=True, exist_ok=True)
        dsn = f"sqlite:///{dbpath}"
        logger.info("ALGOSPHERE_LOCAL_SQLITE enabled — using %s", dsn)
        return dsn

    logger.warning(
        "No DATABASE_URL / TIDB_DSN and ALGOSPHERE_LOCAL_SQLITE not set — "
        "using in-memory SQLite (data not persisted across process restarts)"
    )
    return "sqlite://"
