"""Per-user data endpoints — portfolio, positions, trades, gate-log, regime."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel

from src.api.deps import CurrentUser, DbSession
from src.db.models import UserRole
from src.db.repos import gate_log_repo, portfolio_repo, regime_repo, trade_repo
from src.db.repos import user_repo

router = APIRouter(prefix="/api/users/{user_id}", tags=["users"])


def _check_access(current_user, user_id: str) -> None:
    """Users can only access their own data unless they are admin."""
    if current_user.role != UserRole.admin and current_user.id != user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")


# ---------------------------------------------------------------------------
# Pydantic response models
# ---------------------------------------------------------------------------

class SnapshotOut(BaseModel):
    equity: float | None
    cash: float | None
    buying_power: float | None
    daily_pnl: float | None
    daily_pnl_pct: float | None
    captured_at: str

    model_config = {"from_attributes": True}


class PortfolioOut(BaseModel):
    latest: SnapshotOut | None
    history: list[SnapshotOut]


class PositionOut(BaseModel):
    symbol: str
    side: str
    qty: float
    avg_entry_price: float | None
    current_price: float | None
    unrealized_pnl: float | None
    stop_pct: float | None
    partial_taken: bool

    model_config = {"from_attributes": True}


class TradeOut(BaseModel):
    id: int
    symbol: str
    side: str
    qty: float
    entry_price: float | None
    exit_price: float | None
    pnl: float | None
    pnl_pct: float | None
    exit_reason: str | None
    entered_at: str | None
    exited_at: str | None

    model_config = {"from_attributes": True}


class GateLogOut(BaseModel):
    id: int
    gate: str
    symbol: str | None
    passed: bool
    reason: str | None
    logged_at: str

    model_config = {"from_attributes": True}


class RegimeOut(BaseModel):
    label: str
    spy_score: float | None
    qqq_score: float | None
    vix: float | None
    logged_at: str

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _snap_to_out(snap) -> SnapshotOut:
    return SnapshotOut(
        equity=float(snap.equity) if snap.equity is not None else None,
        cash=float(snap.cash) if snap.cash is not None else None,
        buying_power=float(snap.buying_power) if snap.buying_power is not None else None,
        daily_pnl=float(snap.daily_pnl) if snap.daily_pnl is not None else None,
        daily_pnl_pct=float(snap.daily_pnl_pct) if snap.daily_pnl_pct is not None else None,
        captured_at=snap.captured_at.isoformat(),
    )


def _pos_to_out(pos) -> PositionOut:
    return PositionOut(
        symbol=pos.symbol,
        side=pos.side.value,
        qty=float(pos.qty),
        avg_entry_price=float(pos.avg_entry_price) if pos.avg_entry_price is not None else None,
        current_price=float(pos.current_price) if pos.current_price is not None else None,
        unrealized_pnl=float(pos.unrealized_pnl) if pos.unrealized_pnl is not None else None,
        stop_pct=float(pos.stop_pct) if pos.stop_pct is not None else None,
        partial_taken=pos.partial_taken,
    )


def _trade_to_out(t) -> TradeOut:
    return TradeOut(
        id=t.id,
        symbol=t.symbol,
        side=t.side.value,
        qty=float(t.qty),
        entry_price=float(t.entry_price) if t.entry_price is not None else None,
        exit_price=float(t.exit_price) if t.exit_price is not None else None,
        pnl=float(t.pnl) if t.pnl is not None else None,
        pnl_pct=float(t.pnl_pct) if t.pnl_pct is not None else None,
        exit_reason=t.exit_reason,
        entered_at=t.entered_at.isoformat() if t.entered_at else None,
        exited_at=t.exited_at.isoformat() if t.exited_at else None,
    )


def _gate_to_out(g) -> GateLogOut:
    return GateLogOut(
        id=g.id,
        gate=g.gate,
        symbol=g.symbol,
        passed=g.passed,
        reason=g.reason,
        logged_at=g.logged_at.isoformat(),
    )


def _regime_to_out(r) -> RegimeOut:
    return RegimeOut(
        label=r.label.value,
        spy_score=float(r.spy_score) if r.spy_score is not None else None,
        qqq_score=float(r.qqq_score) if r.qqq_score is not None else None,
        vix=float(r.vix) if r.vix is not None else None,
        logged_at=r.logged_at.isoformat(),
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/portfolio", response_model=PortfolioOut)
async def get_portfolio(
    user_id: str,
    session: DbSession,
    current_user: CurrentUser,
    history_limit: int = Query(default=100, ge=1, le=500),
) -> PortfolioOut:
    _check_access(current_user, user_id)
    latest = portfolio_repo.get_latest_snapshot(session, user_id)
    history = portfolio_repo.get_equity_history(session, user_id, limit=history_limit)
    return PortfolioOut(
        latest=_snap_to_out(latest) if latest else None,
        history=[_snap_to_out(s) for s in history],
    )


@router.get("/positions", response_model=list[PositionOut])
async def get_positions(user_id: str, session: DbSession, current_user: CurrentUser) -> list[PositionOut]:
    _check_access(current_user, user_id)
    return [_pos_to_out(p) for p in portfolio_repo.get_positions(session, user_id)]


@router.get("/trades", response_model=list[TradeOut])
async def get_trades(
    user_id: str,
    session: DbSession,
    current_user: CurrentUser,
    limit: int = Query(default=50, ge=1, le=500),
) -> list[TradeOut]:
    _check_access(current_user, user_id)
    return [_trade_to_out(t) for t in trade_repo.get_trades(session, user_id, limit=limit)]


@router.get("/gate-log", response_model=list[GateLogOut])
async def get_gate_log(
    user_id: str,
    session: DbSession,
    current_user: CurrentUser,
    limit: int = Query(default=50, ge=1, le=500),
) -> list[GateLogOut]:
    _check_access(current_user, user_id)
    return [_gate_to_out(g) for g in gate_log_repo.get_recent(session, user_id, limit=limit)]


@router.get("/regime", response_model=RegimeOut | None)
async def get_regime(user_id: str, session: DbSession, current_user: CurrentUser) -> RegimeOut | None:
    _check_access(current_user, user_id)
    latest = regime_repo.get_latest(session, user_id)
    return _regime_to_out(latest) if latest else None
