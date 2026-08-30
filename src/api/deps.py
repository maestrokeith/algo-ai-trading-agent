"""FastAPI dependencies — JWT auth, DB session, role guards."""
from __future__ import annotations

from typing import Annotated

import jwt as pyjwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from src.auth import decode_token
from src.db.connection import get_session as _get_session
from src.db.models import User, UserRole
from src.db.repos import user_repo

_bearer = HTTPBearer()


async def get_db() -> Session:
    """FastAPI dependency that yields a DB session."""
    with _get_session() as session:
        yield session


DbSession = Annotated[Session, Depends(get_db)]


def _extract_user(
    credentials: HTTPAuthorizationCredentials,
    session: Session,
) -> User:
    try:
        payload = decode_token(credentials.credentials)
    except pyjwt.ExpiredSignatureError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token expired")
    except pyjwt.InvalidTokenError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    user = user_repo.get_by_id(session, payload["sub"])
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    return user


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(_bearer)],
    session: DbSession,
) -> User:
    return _extract_user(credentials, session)


CurrentUser = Annotated[User, Depends(get_current_user)]


async def require_admin(current_user: CurrentUser) -> User:
    if current_user.role != UserRole.admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    return current_user


AdminUser = Annotated[User, Depends(require_admin)]
