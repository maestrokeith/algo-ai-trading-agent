"""Tests for SAR-170: User persistence + JWT auth."""
from __future__ import annotations

import os
import time

import pytest

os.environ.setdefault("TIDB_DSN", "")
os.environ.setdefault("JWT_SECRET", "test-secret-key")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.auth import create_token, decode_token, hash_password, verify_password
from src.db.models import Base, User, UserRole
from src.db.repos import user_repo


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
def trader(session) -> User:
    u = user_repo.create(
        session,
        user_id="trader1",
        email="trader1@test.com",
        hashed_password=hash_password("correct-password"),
        role=UserRole.trader,
        paper=True,
    )
    session.commit()
    return u


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------

def test_hash_password_returns_string():
    h = hash_password("secret")
    assert isinstance(h, str)
    assert h != "secret"


def test_hash_password_unique_per_call():
    h1 = hash_password("secret")
    h2 = hash_password("secret")
    assert h1 != h2  # different salts


def test_verify_password_correct():
    h = hash_password("mypassword")
    assert verify_password("mypassword", h) is True


def test_verify_password_wrong():
    h = hash_password("mypassword")
    assert verify_password("wrongpassword", h) is False


def test_verify_password_empty():
    h = hash_password("mypassword")
    assert verify_password("", h) is False


# ---------------------------------------------------------------------------
# JWT tokens
# ---------------------------------------------------------------------------

def test_create_token_returns_string():
    token = create_token("user1", "trader")
    assert isinstance(token, str)
    assert len(token) > 20


def test_decode_token_roundtrip():
    token = create_token("user42", "admin")
    payload = decode_token(token)
    assert payload["sub"] == "user42"
    assert payload["role"] == "admin"


def test_decode_token_extra_fields():
    token = create_token("user1", "trader", extra={"paper": True})
    payload = decode_token(token)
    assert payload["paper"] is True


def test_decode_token_expired(monkeypatch):
    import importlib

    import jwt as pyjwt
    import src.auth as auth_module

    monkeypatch.setenv("JWT_TTL_SECS", "0")
    importlib.reload(auth_module)
    try:
        token = auth_module.create_token("user1", "trader")
        time.sleep(1)
        with pytest.raises(pyjwt.ExpiredSignatureError):
            auth_module.decode_token(token)
    finally:
        # Restore module to default TTL so later tests are unaffected
        monkeypatch.setenv("JWT_TTL_SECS", "86400")
        importlib.reload(auth_module)


def test_decode_token_invalid():
    import jwt as pyjwt
    with pytest.raises(pyjwt.InvalidTokenError):
        decode_token("not.a.valid.token")


def test_decode_token_tampered():
    import jwt as pyjwt
    token = create_token("user1", "trader")
    tampered = token[:-5] + "XXXXX"
    with pytest.raises(pyjwt.InvalidTokenError):
        decode_token(tampered)


# ---------------------------------------------------------------------------
# User repo
# ---------------------------------------------------------------------------

def test_create_user(session):
    user = user_repo.create(session, user_id="u1", email="u1@test.com")
    session.commit()
    fetched = user_repo.get_by_id(session, "u1")
    assert fetched is not None
    assert fetched.email == "u1@test.com"
    assert fetched.role == UserRole.trader


def test_get_by_email(session, trader):
    found = user_repo.get_by_email(session, "trader1@test.com")
    assert found is not None
    assert found.id == "trader1"


def test_get_by_email_not_found(session):
    assert user_repo.get_by_email(session, "nobody@test.com") is None


def test_get_by_id_not_found(session):
    assert user_repo.get_by_id(session, "ghost") is None


def test_list_all(session, trader):
    user_repo.create(session, user_id="u2", email="u2@test.com")
    session.commit()
    users = user_repo.list_all(session)
    ids = [u.id for u in users]
    assert "trader1" in ids
    assert "u2" in ids


def test_upsert_creates_new(session):
    u = user_repo.upsert(session, user_id="new1", email="new1@test.com")
    session.commit()
    assert user_repo.get_by_id(session, "new1") is not None


def test_upsert_updates_existing(session, trader):
    user_repo.upsert(
        session,
        user_id="trader1",
        email="updated@test.com",
        role=UserRole.admin,
        paper=False,
    )
    session.commit()
    u = user_repo.get_by_id(session, "trader1")
    assert u.email == "updated@test.com"
    assert u.role == UserRole.admin
    assert u.paper is False


def test_upsert_preserves_password_if_not_provided(session, trader):
    original_hash = trader.hashed_password
    user_repo.upsert(session, user_id="trader1", email="trader1@test.com")
    session.commit()
    u = user_repo.get_by_id(session, "trader1")
    assert u.hashed_password == original_hash


def test_login_flow(session, trader):
    """Simulate full login: verify password → issue token → decode token."""
    found = user_repo.get_by_email(session, "trader1@test.com")
    assert found is not None
    assert verify_password("correct-password", found.hashed_password)

    token = create_token(found.id, found.role.value)
    payload = decode_token(token)
    assert payload["sub"] == "trader1"
    assert payload["role"] == "trader"


def test_login_wrong_password(session, trader):
    found = user_repo.get_by_email(session, "trader1@test.com")
    assert not verify_password("wrong-password", found.hashed_password)
