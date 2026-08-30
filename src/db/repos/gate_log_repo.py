"""Gate log repository — 12-gate pipeline decisions."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.db.models import GateLog


def record_gate(
    session: Session,
    *,
    user_id: str,
    gate: str,
    passed: bool,
    symbol: str | None = None,
    reason: str | None = None,
) -> GateLog:
    """Insert a gate decision log entry and return it."""
    entry = GateLog(
        user_id=user_id,
        gate=gate,
        symbol=symbol.upper() if symbol else None,
        passed=passed,
        reason=reason,
    )
    session.add(entry)
    return entry


def get_recent(session: Session, user_id: str, limit: int = 50) -> list[GateLog]:
    """Return the most recent *limit* gate log entries for *user_id*, newest first."""
    return list(
        session.scalars(
            select(GateLog)
            .where(GateLog.user_id == user_id)
            .order_by(GateLog.logged_at.desc(), GateLog.id.desc())
            .limit(limit)
        )
    )


def get_recent_for_gate(
    session: Session, user_id: str, gate: str, limit: int = 20
) -> list[GateLog]:
    """Return recent entries for a specific gate name."""
    return list(
        session.scalars(
            select(GateLog)
            .where(GateLog.user_id == user_id, GateLog.gate == gate)
            .order_by(GateLog.logged_at.desc())
            .limit(limit)
        )
    )
