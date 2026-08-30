"""SQLAlchemy engine + session factory for AlgoSphere.

See :func:`src.db.dsn.resolve_database_dsn` for URL resolution
(``DATABASE_URL``, ``TIDB_DSN``, ``ALGOSPHERE_LOCAL_SQLITE``, in-memory fallback).
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from src.db.dsn import resolve_database_dsn

# ---------------------------------------------------------------------------
# Engine factory
# ---------------------------------------------------------------------------


def _make_engine():
    dsn = resolve_database_dsn()
    is_sqlite = dsn.startswith("sqlite")

    kwargs: dict = {
        "echo": os.environ.get("DB_ECHO", "").lower() in ("1", "true"),
        "future": True,
    }
    if not is_sqlite:
        kwargs["pool_pre_ping"] = True
        kwargs["pool_recycle"] = 1800

    eng = create_engine(dsn, **kwargs)

    # SQLite: enable WAL mode + foreign keys for local dev/test
    if is_sqlite:
        @event.listens_for(eng, "connect")
        def _sqlite_pragmas(conn, _record):
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")

    return eng


engine = _make_engine()
_SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


# ---------------------------------------------------------------------------
# Session context manager
# ---------------------------------------------------------------------------

@contextmanager
def get_session() -> Generator[Session, None, None]:
    """Yield a database session, committing on success or rolling back on error."""
    session: Session = _SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
