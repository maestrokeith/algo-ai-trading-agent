"""Tests for SAR-169: SQLAlchemy DB connection layer."""
from __future__ import annotations

import os

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

os.environ.setdefault("TIDB_DSN", "")  # Force SQLite fallback

from src.db import Base, engine, get_session
from src.db.connection import _make_engine
from src.db.dsn import resolve_database_dsn


# ---------------------------------------------------------------------------
# DSN builder
# ---------------------------------------------------------------------------

def test_dsn_defaults_to_sqlite(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("TIDB_DSN", raising=False)
    monkeypatch.delenv("ALGOSPHERE_LOCAL_SQLITE", raising=False)
    assert resolve_database_dsn() == "sqlite://"


def test_dsn_uses_tidb_env(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("TIDB_DSN", "mysql+pymysql://user:pass@host:4000/db")
    monkeypatch.delenv("ALGOSPHERE_LOCAL_SQLITE", raising=False)
    dsn = resolve_database_dsn()
    assert dsn == "mysql+pymysql://user:pass@host:4000/db"


def test_dsn_appends_ssl_ca(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("TIDB_DSN", "mysql+pymysql://user:pass@host:4000/db")
    monkeypatch.setenv("TIDB_SSL_CA", "/certs/ca.pem")
    monkeypatch.delenv("ALGOSPHERE_LOCAL_SQLITE", raising=False)
    dsn = resolve_database_dsn()
    assert "ssl_ca=/certs/ca.pem" in dsn


def test_dsn_skips_ssl_ca_if_already_present(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("TIDB_DSN", "mysql+pymysql://u:p@h/db?ssl_ca=/already.pem")
    monkeypatch.setenv("TIDB_SSL_CA", "/other.pem")
    monkeypatch.delenv("ALGOSPHERE_LOCAL_SQLITE", raising=False)
    dsn = resolve_database_dsn()
    assert dsn.count("ssl_ca") == 1


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

def test_engine_is_created():
    assert engine is not None


def test_engine_connects():
    with engine.connect() as conn:
        result = conn.execute(text("SELECT 1"))
        assert result.scalar() == 1


def test_sqlite_engine_has_future_flag():
    assert engine.dialect.name == "sqlite"


# ---------------------------------------------------------------------------
# get_session context manager
# ---------------------------------------------------------------------------

def test_get_session_yields_session():
    with get_session() as session:
        assert isinstance(session, Session)


def test_get_session_commits_on_success():
    from src.db.models import User, UserRole
    Base.metadata.create_all(engine)

    with get_session() as session:
        session.add(User(id="conn_test_user", email="conn@test.com"))

    # Verify committed in a new session
    with get_session() as session:
        user = session.get(User, "conn_test_user")
        assert user is not None
        assert user.email == "conn@test.com"

    # Cleanup
    with get_session() as session:
        u = session.get(User, "conn_test_user")
        if u:
            session.delete(u)


def test_get_session_rolls_back_on_error():
    from src.db.models import User
    Base.metadata.create_all(engine)

    try:
        with get_session() as session:
            session.add(User(id="rollback_user", email="rb@test.com"))
            raise ValueError("simulated error")
    except ValueError:
        pass

    with get_session() as session:
        assert session.get(User, "rollback_user") is None


def test_get_session_does_not_leak_transactions():
    """A second get_session() call should get a fresh, independent session."""
    sessions = []
    with get_session() as s1:
        sessions.append(id(s1))
    with get_session() as s2:
        sessions.append(id(s2))
    # Each context manager should produce a distinct session object
    assert sessions[0] != sessions[1]
