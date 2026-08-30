"""User repository — CRUD operations on the ``users`` table."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.db.models import User, UserRole


def get_by_id(session: Session, user_id: str) -> User | None:
    return session.get(User, user_id)


def get_by_email(session: Session, email: str) -> User | None:
    return session.scalar(select(User).where(User.email == email))


def list_all(session: Session) -> list[User]:
    return list(session.scalars(select(User).order_by(User.created_at)))


def create(
    session: Session,
    *,
    user_id: str,
    email: str,
    hashed_password: str | None = None,
    role: UserRole = UserRole.trader,
    paper: bool = True,
    alpaca_key_env: str | None = None,
    alpaca_secret_env: str | None = None,
) -> User:
    user = User(
        id=user_id,
        email=email,
        hashed_password=hashed_password,
        role=role,
        paper=paper,
        alpaca_key_env=alpaca_key_env,
        alpaca_secret_env=alpaca_secret_env,
    )
    session.add(user)
    return user


def upsert(
    session: Session,
    *,
    user_id: str,
    email: str,
    hashed_password: str | None = None,
    role: UserRole = UserRole.trader,
    paper: bool = True,
    alpaca_key_env: str | None = None,
    alpaca_secret_env: str | None = None,
) -> User:
    """Create user if not exists, otherwise update mutable fields."""
    user = get_by_id(session, user_id)
    if user is None:
        return create(
            session,
            user_id=user_id,
            email=email,
            hashed_password=hashed_password,
            role=role,
            paper=paper,
            alpaca_key_env=alpaca_key_env,
            alpaca_secret_env=alpaca_secret_env,
        )
    # Update mutable fields
    user.email = email
    user.role = role
    user.paper = paper
    if hashed_password is not None:
        user.hashed_password = hashed_password
    if alpaca_key_env is not None:
        user.alpaca_key_env = alpaca_key_env
    if alpaca_secret_env is not None:
        user.alpaca_secret_env = alpaca_secret_env
    return user
