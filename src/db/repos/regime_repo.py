"""Regime log repository — market regime state tracking."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.db.models import RegimeLabel, RegimeLog


def record_regime(
    session: Session,
    *,
    user_id: str,
    label: str,
    spy_score: float | None = None,
    qqq_score: float | None = None,
    vix: float | None = None,
    state_vector: list[float] | None = None,
) -> RegimeLog:
    """Insert a regime snapshot and return it."""
    entry = RegimeLog(
        user_id=user_id,
        label=RegimeLabel(label.lower()),
        spy_score=spy_score,
        qqq_score=qqq_score,
        vix=vix,
        state_vector=state_vector,
    )
    session.add(entry)
    return entry


def get_latest(session: Session, user_id: str) -> RegimeLog | None:
    """Return the most recent regime entry for *user_id*."""
    return session.scalar(
        select(RegimeLog)
        .where(RegimeLog.user_id == user_id)
        .order_by(RegimeLog.logged_at.desc())
        .limit(1)
    )


def get_history(session: Session, user_id: str, limit: int = 100) -> list[RegimeLog]:
    """Return the last *limit* regime entries, newest first."""
    return list(
        session.scalars(
            select(RegimeLog)
            .where(RegimeLog.user_id == user_id)
            .order_by(RegimeLog.logged_at.desc())
            .limit(limit)
        )
    )
