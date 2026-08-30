"""Tests for SAR-171: Portfolio + position persistence."""
from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("TIDB_DSN", "")

from src.db.models import Base, User, UserRole
from src.db.repos import portfolio_repo


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def engine():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, future=True)
    Base.metadata.create_all(eng)
    yield eng
    Base.metadata.drop_all(eng)
    eng.dispose()


@pytest.fixture()
def session(engine):
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    with Session() as s:
        yield s


@pytest.fixture()
def user(session) -> User:
    u = User(id="trader1", email="t1@test.com")
    session.add(u)
    session.commit()
    return u


@pytest.fixture()
def user2(session) -> User:
    u = User(id="trader2", email="t2@test.com")
    session.add(u)
    session.commit()
    return u


# ---------------------------------------------------------------------------
# Portfolio snapshots
# ---------------------------------------------------------------------------

def test_snapshot_portfolio(session, user):
    snap = portfolio_repo.snapshot_portfolio(
        session, user_id="trader1", equity=100_000.0, cash=50_000.0, daily_pnl=500.0
    )
    session.commit()
    assert snap.id is not None
    assert float(snap.equity) == pytest.approx(100_000.0)


def test_get_latest_snapshot(session, user):
    portfolio_repo.snapshot_portfolio(session, user_id="trader1", equity=99_000.0)
    session.commit()
    portfolio_repo.snapshot_portfolio(session, user_id="trader1", equity=100_500.0)
    session.commit()

    latest = portfolio_repo.get_latest_snapshot(session, "trader1")
    assert latest is not None
    assert float(latest.equity) == pytest.approx(100_500.0)


def test_get_latest_snapshot_none_when_empty(session, user):
    assert portfolio_repo.get_latest_snapshot(session, "trader1") is None


def test_get_equity_history_ordered(session, user):
    for equity in [99_000, 99_500, 100_000]:
        portfolio_repo.snapshot_portfolio(session, user_id="trader1", equity=equity)
        session.commit()

    history = portfolio_repo.get_equity_history(session, "trader1")
    equities = [float(s.equity) for s in history]
    assert equities == sorted(equities)  # oldest-first


def test_get_equity_history_limit(session, user):
    for i in range(10):
        portfolio_repo.snapshot_portfolio(session, user_id="trader1", equity=float(i))
        session.commit()

    history = portfolio_repo.get_equity_history(session, "trader1", limit=5)
    assert len(history) == 5


def test_snapshot_multi_user_isolation(session, user, user2):
    portfolio_repo.snapshot_portfolio(session, user_id="trader1", equity=10_000.0)
    portfolio_repo.snapshot_portfolio(session, user_id="trader2", equity=20_000.0)
    session.commit()

    snap1 = portfolio_repo.get_latest_snapshot(session, "trader1")
    snap2 = portfolio_repo.get_latest_snapshot(session, "trader2")
    assert float(snap1.equity) == pytest.approx(10_000.0)
    assert float(snap2.equity) == pytest.approx(20_000.0)


# ---------------------------------------------------------------------------
# Positions
# ---------------------------------------------------------------------------

def test_upsert_position_creates(session, user):
    pos = portfolio_repo.upsert_position(
        session, user_id="trader1", symbol="AAPL", qty=10.0, avg_entry_price=175.0
    )
    session.commit()
    assert pos.id is not None
    assert pos.symbol == "AAPL"
    assert float(pos.qty) == pytest.approx(10.0)


def test_upsert_position_updates(session, user):
    portfolio_repo.upsert_position(
        session, user_id="trader1", symbol="AAPL", qty=10.0, avg_entry_price=175.0
    )
    session.commit()

    updated = portfolio_repo.upsert_position(
        session, user_id="trader1", symbol="AAPL", qty=15.0, avg_entry_price=177.0
    )
    session.commit()

    positions = portfolio_repo.get_positions(session, "trader1")
    assert len(positions) == 1
    assert float(positions[0].qty) == pytest.approx(15.0)


def test_upsert_position_symbol_case_insensitive(session, user):
    portfolio_repo.upsert_position(session, user_id="trader1", symbol="aapl", qty=5.0)
    session.commit()
    portfolio_repo.upsert_position(session, user_id="trader1", symbol="AAPL", qty=8.0)
    session.commit()

    positions = portfolio_repo.get_positions(session, "trader1")
    assert len(positions) == 1
    assert float(positions[0].qty) == pytest.approx(8.0)


def test_get_positions_returns_all(session, user):
    for sym in ["AAPL", "MSFT", "TSLA"]:
        portfolio_repo.upsert_position(session, user_id="trader1", symbol=sym, qty=5.0)
    session.commit()

    positions = portfolio_repo.get_positions(session, "trader1")
    assert len(positions) == 3
    assert {p.symbol for p in positions} == {"AAPL", "MSFT", "TSLA"}


def test_get_positions_multi_user_isolation(session, user, user2):
    portfolio_repo.upsert_position(session, user_id="trader1", symbol="AAPL", qty=5.0)
    portfolio_repo.upsert_position(session, user_id="trader2", symbol="NVDA", qty=3.0)
    session.commit()

    t1_pos = portfolio_repo.get_positions(session, "trader1")
    t2_pos = portfolio_repo.get_positions(session, "trader2")
    assert len(t1_pos) == 1 and t1_pos[0].symbol == "AAPL"
    assert len(t2_pos) == 1 and t2_pos[0].symbol == "NVDA"


def test_remove_position(session, user):
    portfolio_repo.upsert_position(session, user_id="trader1", symbol="AAPL", qty=5.0)
    session.commit()

    removed = portfolio_repo.remove_position(session, "trader1", "AAPL")
    session.commit()

    assert removed is True
    assert portfolio_repo.get_positions(session, "trader1") == []


def test_remove_position_not_found(session, user):
    removed = portfolio_repo.remove_position(session, "trader1", "GHOST")
    assert removed is False


def test_clear_positions(session, user):
    for sym in ["AAPL", "MSFT", "TSLA"]:
        portfolio_repo.upsert_position(session, user_id="trader1", symbol=sym, qty=1.0)
    session.commit()

    count = portfolio_repo.clear_positions(session, "trader1")
    session.commit()
    assert count == 3
    assert portfolio_repo.get_positions(session, "trader1") == []
