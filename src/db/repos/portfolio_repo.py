"""Portfolio and position repository."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.db.models import PortfolioSnapshot, Position, TradeSide


# ---------------------------------------------------------------------------
# Portfolio snapshots
# ---------------------------------------------------------------------------

def snapshot_portfolio(
    session: Session,
    *,
    user_id: str,
    equity: float | None = None,
    cash: float | None = None,
    buying_power: float | None = None,
    daily_pnl: float | None = None,
    daily_pnl_pct: float | None = None,
) -> PortfolioSnapshot:
    """Insert a new portfolio snapshot and return it."""
    snap = PortfolioSnapshot(
        user_id=user_id,
        equity=equity,
        cash=cash,
        buying_power=buying_power,
        daily_pnl=daily_pnl,
        daily_pnl_pct=daily_pnl_pct,
    )
    session.add(snap)
    return snap


def get_latest_snapshot(session: Session, user_id: str) -> PortfolioSnapshot | None:
    """Return the most recent snapshot for *user_id*."""
    return session.scalar(
        select(PortfolioSnapshot)
        .where(PortfolioSnapshot.user_id == user_id)
        .order_by(PortfolioSnapshot.captured_at.desc())
        .limit(1)
    )


def get_equity_history(
    session: Session, user_id: str, limit: int = 100
) -> list[PortfolioSnapshot]:
    """Return the last *limit* snapshots ordered oldest-first (for charting)."""
    subq = (
        select(PortfolioSnapshot)
        .where(PortfolioSnapshot.user_id == user_id)
        .order_by(PortfolioSnapshot.captured_at.desc())
        .limit(limit)
        .subquery()
    )
    # Re-order ascending for chart consumption
    return list(
        session.scalars(
            select(PortfolioSnapshot)
            .where(PortfolioSnapshot.user_id == user_id)
            .order_by(PortfolioSnapshot.captured_at.desc())
            .limit(limit)
        )
    )[::-1]


# ---------------------------------------------------------------------------
# Positions
# ---------------------------------------------------------------------------

def upsert_position(
    session: Session,
    *,
    user_id: str,
    symbol: str,
    side: str = "long",
    qty: float,
    avg_entry_price: float | None = None,
    current_price: float | None = None,
    unrealized_pnl: float | None = None,
    stop_pct: float | None = None,
    partial_taken: bool = False,
    trail_high: float | None = None,
    entered_at: datetime | None = None,
) -> Position:
    """Create or update an open position for *user_id* / *symbol*."""
    existing = session.scalar(
        select(Position)
        .where(Position.user_id == user_id, Position.symbol == symbol.upper())
    )
    trade_side = TradeSide(side.lower())
    if existing is None:
        pos = Position(
            user_id=user_id,
            symbol=symbol.upper(),
            side=trade_side,
            qty=qty,
            avg_entry_price=avg_entry_price,
            current_price=current_price,
            unrealized_pnl=unrealized_pnl,
            stop_pct=stop_pct,
            partial_taken=partial_taken,
            trail_high=trail_high,
            entered_at=entered_at,
        )
        session.add(pos)
        return pos

    existing.side = trade_side
    existing.qty = qty
    if avg_entry_price is not None:
        existing.avg_entry_price = avg_entry_price
    if current_price is not None:
        existing.current_price = current_price
    if unrealized_pnl is not None:
        existing.unrealized_pnl = unrealized_pnl
    if stop_pct is not None:
        existing.stop_pct = stop_pct
    existing.partial_taken = partial_taken
    if trail_high is not None:
        existing.trail_high = trail_high
    existing.updated_at = datetime.now(timezone.utc)
    return existing


def get_positions(session: Session, user_id: str) -> list[Position]:
    """Return all open positions for *user_id*."""
    return list(
        session.scalars(
            select(Position)
            .where(Position.user_id == user_id)
            .order_by(Position.entered_at.desc())
        )
    )


def remove_position(session: Session, user_id: str, symbol: str) -> bool:
    """Delete a position. Returns True if it existed."""
    pos = session.scalar(
        select(Position)
        .where(Position.user_id == user_id, Position.symbol == symbol.upper())
    )
    if pos is None:
        return False
    session.delete(pos)
    return True


def clear_positions(session: Session, user_id: str) -> int:
    """Delete all positions for *user_id*. Returns count deleted."""
    positions = get_positions(session, user_id)
    for pos in positions:
        session.delete(pos)
    return len(positions)
