"""SQLAlchemy ORM models for AlgoSphere.

All tables are designed for TiDB (MySQL-compatible).  Vector columns use
``TiDBVector`` which falls back to TEXT on SQLite for CI compatibility.

Tables
------
- users              — trader accounts (auth + config)
- portfolio_snapshots — point-in-time equity/cash snapshots per user
- positions          — current open positions per user
- trades             — completed trade history (vector-ready for agents)
- regime_log         — market regime state over time (vector-ready)
- gate_log           — 12-gate pipeline decisions per loop
"""
from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from src.db.vector_type import TiDBVector


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------

class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class UserRole(str, enum.Enum):
    admin = "admin"
    trader = "trader"


class TradeSide(str, enum.Enum):
    long = "long"
    short = "short"


class RegimeLabel(str, enum.Enum):
    bullish = "bullish"
    neutral = "neutral"
    bearish = "bearish"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class User(Base):
    """A trader account in AlgoSphere."""

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    hashed_password: Mapped[str | None] = mapped_column(String(255), nullable=True)
    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole), nullable=False, default=UserRole.trader
    )
    paper: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    alpaca_key_env: Mapped[str | None] = mapped_column(String(128), nullable=True)
    alpaca_secret_env: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    snapshots: Mapped[list[PortfolioSnapshot]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    positions: Mapped[list[Position]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    trades: Mapped[list[Trade]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    regime_logs: Mapped[list[RegimeLog]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    gate_logs: Mapped[list[GateLog]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<User id={self.id!r} role={self.role.value}>"


class PortfolioSnapshot(Base):
    """Point-in-time equity/cash snapshot for a user."""

    __tablename__ = "portfolio_snapshots"
    __table_args__ = (
        Index("idx_snapshot_user_time", "user_id", "captured_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    equity: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    cash: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    buying_power: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    daily_pnl: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    daily_pnl_pct: Mapped[float | None] = mapped_column(Numeric(8, 4), nullable=True)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    user: Mapped[User] = relationship(back_populates="snapshots")

    def __repr__(self) -> str:
        return f"<PortfolioSnapshot user={self.user_id!r} equity={self.equity}>"


class Position(Base):
    """Current open position for a user."""

    __tablename__ = "positions"
    __table_args__ = (
        Index("idx_position_user", "user_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    side: Mapped[TradeSide] = mapped_column(Enum(TradeSide), nullable=False, default=TradeSide.long)
    qty: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False)
    avg_entry_price: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    current_price: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    unrealized_pnl: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    stop_pct: Mapped[float | None] = mapped_column(Numeric(8, 4), nullable=True)
    partial_taken: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    trail_high: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    entered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    user: Mapped[User] = relationship(back_populates="positions")

    def __repr__(self) -> str:
        return f"<Position user={self.user_id!r} symbol={self.symbol!r} qty={self.qty}>"


class Trade(Base):
    """Completed trade record.  ``setup_vector`` reserved for agent similarity search."""

    __tablename__ = "trades"
    __table_args__ = (
        Index("idx_trade_user", "user_id"),
        Index("idx_trade_user_time", "user_id", "exited_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    side: Mapped[TradeSide] = mapped_column(Enum(TradeSide), nullable=False, default=TradeSide.long)
    qty: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False)
    entry_price: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    exit_price: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    pnl: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    pnl_pct: Mapped[float | None] = mapped_column(Numeric(8, 4), nullable=True)
    exit_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    entered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    exited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Reserved for agent embedding of market conditions at trade entry
    setup_vector: Mapped[list[float] | None] = mapped_column(TiDBVector(1536), nullable=True)

    user: Mapped[User] = relationship(back_populates="trades")

    def __repr__(self) -> str:
        return f"<Trade user={self.user_id!r} symbol={self.symbol!r} pnl={self.pnl}>"


class RegimeLog(Base):
    """Market regime state snapshot.  ``state_vector`` reserved for agent retrieval."""

    __tablename__ = "regime_log"
    __table_args__ = (
        Index("idx_regime_user_time", "user_id", "logged_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    label: Mapped[RegimeLabel] = mapped_column(Enum(RegimeLabel), nullable=False)
    spy_score: Mapped[float | None] = mapped_column(Numeric(6, 4), nullable=True)
    qqq_score: Mapped[float | None] = mapped_column(Numeric(6, 4), nullable=True)
    vix: Mapped[float | None] = mapped_column(Numeric(8, 2), nullable=True)
    logged_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    # Reserved for agent similarity search ("find regimes like now")
    state_vector: Mapped[list[float] | None] = mapped_column(TiDBVector(256), nullable=True)

    user: Mapped[User] = relationship(back_populates="regime_logs")

    def __repr__(self) -> str:
        return f"<RegimeLog user={self.user_id!r} label={self.label.value}>"


class GateLog(Base):
    """12-gate pipeline decision log entry."""

    __tablename__ = "gate_log"
    __table_args__ = (
        Index("idx_gate_user_time", "user_id", "logged_at"),
        Index("idx_gate_user_gate", "user_id", "gate"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    gate: Mapped[str] = mapped_column(String(64), nullable=False)
    symbol: Mapped[str | None] = mapped_column(String(16), nullable=True)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    logged_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    user: Mapped[User] = relationship(back_populates="gate_logs")

    def __repr__(self) -> str:
        return f"<GateLog user={self.user_id!r} gate={self.gate!r} passed={self.passed}>"
