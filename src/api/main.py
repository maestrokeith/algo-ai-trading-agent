"""AlgoSphere FastAPI application."""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.api.routers import admin, auth, users
from src.db import Base, engine

# Create tables on startup if they don't exist (dev convenience; use Alembic in prod)
Base.metadata.create_all(engine)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Keep API readiness independent from trading services and background jobs."""
    yield


app = FastAPI(
    title="AlgoSphere API",
    version="0.1.0",
    description="Multi-user algorithmic trading dashboard API",
    lifespan=lifespan,
)

_allowed_origins = os.environ.get(
    "CORS_ORIGINS",
    "http://localhost:5173,http://127.0.0.1:5173,http://localhost:3000,http://127.0.0.1:3000",
).split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in _allowed_origins],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(users.router)
app.include_router(admin.router)


@app.get("/healthz", tags=["meta"])
async def healthz() -> dict:
    return {"status": "ok"}
