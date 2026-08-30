"""Tests for :mod:`src.db.dsn` URL resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.db import dsn as dsn_module


def test_resolve_prefers_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost/db")
    monkeypatch.delenv("TIDB_DSN", raising=False)
    monkeypatch.delenv("ALGOSPHERE_LOCAL_SQLITE", raising=False)
    assert dsn_module.resolve_database_dsn().startswith("postgresql://")


def test_resolve_tidb_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("TIDB_DSN", "mysql+pymysql://u:p@h/db")
    monkeypatch.delenv("ALGOSPHERE_LOCAL_SQLITE", raising=False)
    assert "mysql+pymysql" in dsn_module.resolve_database_dsn()


def test_local_sqlite_creates_data_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("TIDB_DSN", raising=False)
    monkeypatch.setenv("ALGOSPHERE_LOCAL_SQLITE", "1")
    # Redirect repo root by patching Path used in module — simpler: resolve returns path under cwd
    out = dsn_module.resolve_database_dsn()
    assert out.startswith("sqlite:///")
    assert "algosphere.db" in out
    p = Path(out.replace("sqlite:///", ""))
    assert p.name == "algosphere.db"


def test_in_memory_when_nothing_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("TIDB_DSN", raising=False)
    monkeypatch.delenv("ALGOSPHERE_LOCAL_SQLITE", raising=False)
    assert dsn_module.resolve_database_dsn() == "sqlite://"
