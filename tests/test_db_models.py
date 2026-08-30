"""Tests for SAR-168: TiDB schema + Alembic migrations.

Uses SQLite in-memory for CI — no TiDB cluster required.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

# Ensure SQLite fallback for tests
os.environ.setdefault("TIDB_DSN", "")

from src.db.models import (
    Base,
    GateLog,
    PortfolioSnapshot,
    Position,
    RegimeLabel,
    RegimeLog,
    Trade,
    TradeSide,
    User,
    UserRole,
)


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
    u = User(
        id="trader1",
        email="trader1@algo.com",
        role=UserRole.trader,
        paper=True,
    )
    session.add(u)
    session.commit()
    return u


# ---------------------------------------------------------------------------
# Schema / table existence
# ---------------------------------------------------------------------------

def test_all_tables_created(engine):
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    expected = {"users", "portfolio_snapshots", "positions", "trades", "regime_log", "gate_log"}
    assert expected <= tables


def test_users_columns(engine):
    insp = inspect(engine)
    cols = {c["name"] for c in insp.get_columns("users")}
    assert {"id", "email", "hashed_password", "role", "paper", "created_at"} <= cols


def test_trades_vector_column(engine):
    insp = inspect(engine)
    cols = {c["name"] for c in insp.get_columns("trades")}
    assert "setup_vector" in cols


def test_regime_log_vector_column(engine):
    insp = inspect(engine)
    cols = {c["name"] for c in insp.get_columns("regime_log")}
    assert "state_vector" in cols


def test_indexes_created(engine):
    insp = inspect(engine)
    trade_indexes = {i["name"] for i in insp.get_indexes("trades")}
    assert "idx_trade_user" in trade_indexes
    assert "idx_trade_user_time" in trade_indexes

    gate_indexes = {i["name"] for i in insp.get_indexes("gate_log")}
    assert "idx_gate_user_time" in gate_indexes
    assert "idx_gate_user_gate" in gate_indexes


# ---------------------------------------------------------------------------
# User model
# ---------------------------------------------------------------------------

def test_create_user(session):
    u = User(id="u1", email="u1@test.com", role=UserRole.admin, paper=False)
    session.add(u)
    session.commit()

    fetched = session.get(User, "u1")
    assert fetched is not None
    assert fetched.email == "u1@test.com"
    assert fetched.role == UserRole.admin
    assert fetched.paper is False


def test_user_default_role_is_trader(session):
    u = User(id="u2", email="u2@test.com")
    session.add(u)
    session.commit()
    assert session.get(User, "u2").role == UserRole.trader


def test_user_email_unique_constraint(session):
    session.add(User(id="u3", email="same@test.com"))
    session.commit()
    session.add(User(id="u4", email="same@test.com"))
    with pytest.raises(Exception):
        session.commit()


# ---------------------------------------------------------------------------
# PortfolioSnapshot
# ---------------------------------------------------------------------------

def test_create_portfolio_snapshot(session, user):
    snap = PortfolioSnapshot(
        user_id=user.id,
        equity=100_000.00,
        cash=50_000.00,
        daily_pnl=1_234.56,
    )
    session.add(snap)
    session.commit()

    fetched = session.get(PortfolioSnapshot, snap.id)
    assert fetched is not None
    assert float(fetched.equity) == pytest.approx(100_000.00)
    assert float(fetched.daily_pnl) == pytest.approx(1_234.56)


def test_portfolio_snapshot_fk_cascade(session, user):
    snap = PortfolioSnapshot(user_id=user.id, equity=1000)
    session.add(snap)
    session.commit()
    snap_id = snap.id

    session.delete(user)
    session.commit()
    assert session.get(PortfolioSnapshot, snap_id) is None


# ---------------------------------------------------------------------------
# Position
# ---------------------------------------------------------------------------

def test_create_position(session, user):
    pos = Position(
        user_id=user.id,
        symbol="AAPL",
        side=TradeSide.long,
        qty=10.0,
        avg_entry_price=175.50,
    )
    session.add(pos)
    session.commit()

    fetched = session.get(Position, pos.id)
    assert fetched.symbol == "AAPL"
    assert fetched.side == TradeSide.long
    assert float(fetched.qty) == pytest.approx(10.0)


def test_position_multi_user_isolation(session):
    u1 = User(id="mu1", email="mu1@test.com")
    u2 = User(id="mu2", email="mu2@test.com")
    session.add_all([u1, u2])
    session.commit()

    session.add(Position(user_id="mu1", symbol="TSLA", side=TradeSide.long, qty=5))
    session.add(Position(user_id="mu2", symbol="NVDA", side=TradeSide.long, qty=3))
    session.commit()

    from sqlalchemy import select
    u1_positions = session.scalars(
        select(Position).where(Position.user_id == "mu1")
    ).all()
    assert len(u1_positions) == 1
    assert u1_positions[0].symbol == "TSLA"


# ---------------------------------------------------------------------------
# Trade (including vector column)
# ---------------------------------------------------------------------------

def test_create_trade_without_vector(session, user):
    trade = Trade(
        user_id=user.id,
        symbol="MSFT",
        side=TradeSide.long,
        qty=20.0,
        entry_price=400.00,
        exit_price=415.00,
        pnl=300.00,
        exit_reason="trail_stop",
    )
    session.add(trade)
    session.commit()

    fetched = session.get(Trade, trade.id)
    assert fetched.exit_reason == "trail_stop"
    assert float(fetched.pnl) == pytest.approx(300.00)
    assert fetched.setup_vector is None


def test_trade_with_setup_vector(session, user):
    vector = [0.1] * 1536
    trade = Trade(
        user_id=user.id,
        symbol="GOOG",
        side=TradeSide.long,
        qty=5.0,
        setup_vector=vector,
    )
    session.add(trade)
    session.commit()

    fetched = session.get(Trade, trade.id)
    assert fetched.setup_vector is not None
    assert len(fetched.setup_vector) == 1536
    assert fetched.setup_vector[0] == pytest.approx(0.1)


# ---------------------------------------------------------------------------
# RegimeLog
# ---------------------------------------------------------------------------

def test_create_regime_log(session, user):
    regime = RegimeLog(
        user_id=user.id,
        label=RegimeLabel.bullish,
        spy_score=0.72,
        qqq_score=0.68,
        vix=14.2,
    )
    session.add(regime)
    session.commit()

    fetched = session.get(RegimeLog, regime.id)
    assert fetched.label == RegimeLabel.bullish
    assert float(fetched.spy_score) == pytest.approx(0.72)


def test_regime_log_state_vector(session, user):
    vector = [0.5] * 256
    regime = RegimeLog(
        user_id=user.id,
        label=RegimeLabel.neutral,
        state_vector=vector,
    )
    session.add(regime)
    session.commit()

    fetched = session.get(RegimeLog, regime.id)
    assert len(fetched.state_vector) == 256


# ---------------------------------------------------------------------------
# GateLog
# ---------------------------------------------------------------------------

def test_create_gate_log(session, user):
    entry = GateLog(
        user_id=user.id,
        gate="regime_filter",
        symbol="AAPL",
        passed=True,
        reason="bullish regime",
    )
    session.add(entry)
    session.commit()

    fetched = session.get(GateLog, entry.id)
    assert fetched.gate == "regime_filter"
    assert fetched.passed is True
    assert fetched.reason == "bullish regime"


def test_gate_log_failed_gate(session, user):
    entry = GateLog(
        user_id=user.id,
        gate="vix_filter",
        symbol=None,
        passed=False,
        reason="VIX > 30",
    )
    session.add(entry)
    session.commit()

    fetched = session.get(GateLog, entry.id)
    assert fetched.passed is False
    assert fetched.symbol is None


def test_gate_log_multi_entries(session, user):
    from sqlalchemy import select
    gates = ["regime_filter", "vix_filter", "pdt_filter"]
    for gate in gates:
        session.add(GateLog(user_id=user.id, gate=gate, passed=True))
    session.commit()

    count = session.scalar(
        select(GateLog).where(GateLog.user_id == user.id).with_only_columns(
            __import__("sqlalchemy").func.count()
        )
    )
    assert count == 3
