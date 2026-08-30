"""Admin router — cross-user overview (admin role only)."""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from src.api.deps import AdminUser, DbSession
from src.db.repos import user_repo, portfolio_repo

router = APIRouter(prefix="/api/admin", tags=["admin"])


class UserSummary(BaseModel):
    id: str
    email: str
    role: str
    paper: bool
    equity: float | None


@router.get("/users", response_model=list[UserSummary])
async def list_users(session: DbSession, _admin: AdminUser) -> list[UserSummary]:
    users = user_repo.list_all(session)
    summaries = []
    for user in users:
        snap = portfolio_repo.get_latest_snapshot(session, user.id)
        summaries.append(UserSummary(
            id=user.id,
            email=user.email,
            role=user.role.value,
            paper=user.paper,
            equity=float(snap.equity) if snap and snap.equity is not None else None,
        ))
    return summaries
