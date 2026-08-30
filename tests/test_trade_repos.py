"""Tests for SAR-172: Trade + gate log + regime persistence."""
from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("TIDB_DSN", "")

from src.db.models import Base, RegimeLabel, User
from src.db.repos import gate_log_repo, regime_repo, trade_repo


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
# Trade repo
# ---------------------------------------------------------------------------

def test_record_trade(session, user):
    trade = trade_repo.record_trade(
        session,
        user_id="trader1",
        symbol="AAPL",
        side="long",
        qty=10.0,
        entry_price=175.0,
        exit_price=182.0,
        pnl=70.0,
        exit_reason="trail_stop",
    )
    session.commit()
    assert trade.id is not None
    assert trade.symbol == "AAPL"
    assert float(trade.pnl) == pytest.approx(70.0)
    assert trade.exit_reason == "trail_stop"


def test_record_trade_symbol_uppercased(session, user):
    trade = trade_repo.record_trade(session, user_id="trader1", symbol="msft", qty=5.0)
    session.commit()
    assert trade.symbol == "MSFT"


def test_record_trade_with_vector(session, user):
    vector = [0.25] * 1536
    trade = trade_repo.record_trade(
        session, user_id="trader1", symbol="GOOG", qty=2.0, setup_vector=vector
    )
    session.commit()
    fetched = trade_repo.get_trade_by_id(session, trade.id)
    assert fetched.setup_vector is not None
    assert len(fetched.setup_vector) == 1536


def test_get_trades_newest_first(session, user):
    now = datetime.now(timezone.utc)
    for i, sym in enumerate(["AAPL", "MSFT", "TSLA"]):
        t = trade_repo.record_trade(session, user_id="trader1", symbol=sym, qty=1.0)
        t.exited_at = datetime(2026, 1, i + 1, tzinfo=timezone.utc)
    session.commit()

    trades = trade_repo.get_trades(session, "trader1")
    symbols = [t.symbol for t in trades]
    assert symbols == ["TSLA", "MSFT", "AAPL"]


def test_get_trades_limit(session, user):
    for i in range(10):
        trade_repo.record_trade(session, user_id="trader1", symbol=f"SYM{i}", qty=1.0)
    session.commit()
    assert len(trade_repo.get_trades(session, "trader1", limit=3)) == 3


def test_get_trades_multi_user_isolation(session, user, user2):
    trade_repo.record_trade(session, user_id="trader1", symbol="AAPL", qty=5.0)
    trade_repo.record_trade(session, user_id="trader2", symbol="NVDA", qty=3.0)
    session.commit()

    t1 = trade_repo.get_trades(session, "trader1")
    t2 = trade_repo.get_trades(session, "trader2")
    assert len(t1) == 1 and t1[0].symbol == "AAPL"
    assert len(t2) == 1 and t2[0].symbol == "NVDA"


# ---------------------------------------------------------------------------
# Gate log repo
# ---------------------------------------------------------------------------

def test_record_gate_passed(session, user):
    entry = gate_log_repo.record_gate(
        session, user_id="trader1", gate="regime_filter", passed=True,
        symbol="AAPL", reason="bullish"
    )
    session.commit()
    assert entry.id is not None
    assert entry.passed is True
    assert entry.symbol == "AAPL"


def test_record_gate_failed(session, user):
    entry = gate_log_repo.record_gate(
        session, user_id="trader1", gate="vix_filter", passed=False, reason="VIX=32"
    )
    session.commit()
    assert entry.passed is False
    assert entry.symbol is None


def test_get_recent_gate_log(session, user):
    gates = ["regime_filter", "vix_filter", "pdt_filter"]
    for gate in gates:
        gate_log_repo.record_gate(session, user_id="trader1", gate=gate, passed=True)
    session.commit()

    recent = gate_log_repo.get_recent(session, "trader1")
    assert len(recent) == 3


def test_get_recent_limit(session, user):
    for i in range(10):
        gate_log_repo.record_gate(session, user_id="trader1", gate=f"gate_{i}", passed=True)
    session.commit()
    assert len(gate_log_repo.get_recent(session, "trader1", limit=4)) == 4


def test_get_recent_for_gate(session, user):
    for _ in range(3):
        gate_log_repo.record_gate(session, user_id="trader1", gate="vix_filter", passed=True)
    gate_log_repo.record_gate(session, user_id="trader1", gate="other_gate", passed=True)
    session.commit()

    vix_entries = gate_log_repo.get_recent_for_gate(session, "trader1", "vix_filter")
    assert len(vix_entries) == 3


def test_gate_log_multi_user_isolation(session, user, user2):
    gate_log_repo.record_gate(session, user_id="trader1", gate="g1", passed=True)
    gate_log_repo.record_gate(session, user_id="trader2", gate="g1", passed=False)
    session.commit()

    t1 = gate_log_repo.get_recent(session, "trader1")
    t2 = gate_log_repo.get_recent(session, "trader2")
    assert t1[0].passed is True
    assert t2[0].passed is False


# ---------------------------------------------------------------------------
# Regime repo
# ---------------------------------------------------------------------------

def test_record_regime(session, user):
    entry = regime_repo.record_regime(
        session, user_id="trader1", label="bullish", spy_score=0.72, qqq_score=0.68, vix=14.2
    )
    session.commit()
    assert entry.id is not None
    assert entry.label == RegimeLabel.bullish
    assert float(entry.spy_score) == pytest.approx(0.72)


def test_record_regime_with_vector(session, user):
    vector = [0.5] * 256
    entry = regime_repo.record_regime(
        session, user_id="trader1", label="neutral", state_vector=vector
    )
    session.commit()
    fetched = session.get(entry.__class__, entry.id)
    assert len(fetched.state_vector) == 256


def test_get_latest_regime(session, user):
    regime_repo.record_regime(session, user_id="trader1", label="bearish")
    session.commit()
    regime_repo.record_regime(session, user_id="trader1", label="bullish")
    session.commit()

    latest = regime_repo.get_latest(session, "trader1")
    assert latest.label == RegimeLabel.bullish


def test_get_latest_regime_none_when_empty(session, user):
    assert regime_repo.get_latest(session, "trader1") is None


def test_get_regime_history(session, user):
    for label in ["bearish", "neutral", "bullish"]:
        regime_repo.record_regime(session, user_id="trader1", label=label)
        session.commit()

    history = regime_repo.get_history(session, "trader1")
    assert len(history) == 3


def test_regime_multi_user_isolation(session, user, user2):
    regime_repo.record_regime(session, user_id="trader1", label="bullish")
    regime_repo.record_regime(session, user_id="trader2", label="bearish")
    session.commit()

    assert regime_repo.get_latest(session, "trader1").label == RegimeLabel.bullish
    assert regime_repo.get_latest(session, "trader2").label == RegimeLabel.bearish
