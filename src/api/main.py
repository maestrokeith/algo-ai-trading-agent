"""AlgoSphere FastAPI application."""
from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from src.api.routers import admin, auth, command, research, users
from src.db import Base, engine

Base.metadata.create_all(engine)

ROOT_DIR = Path(__file__).resolve().parents[2]
WEB_DIST = ROOT_DIR / "frontend" / "dist"


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Keep API readiness independent from trading services and background jobs."""
    yield


app = FastAPI(
    title="AlgoSphere API",
    version="0.4.0",
    description="Paper-only quantitative research command center and account data services",
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
app.include_router(research.router)
app.include_router(command.router)


@app.get("/healthz", tags=["meta"])
async def healthz() -> dict:
    return {
        "status": "ok",
        "research_mode": "paper",
        "live_execution": False,
        "command_center": True,
        "web_ui": WEB_DIST.exists(),
    }


if (WEB_DIST / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=WEB_DIST / "assets"), name="assets")


@app.get("/{full_path:path}", include_in_schema=False)
async def serve_web_app(full_path: str):
    """Serve the Vite production build and support React Router history paths."""
    if full_path.startswith(("api/", "auth/")):
        raise HTTPException(status_code=404, detail="Not found")

    if not WEB_DIST.is_dir():
        raise HTTPException(status_code=503, detail="Web UI has not been built")

    root = WEB_DIST.resolve()
    candidate = (WEB_DIST / full_path).resolve() if full_path else root / "index.html"

    if candidate.is_relative_to(root) and candidate.is_file():
        return FileResponse(candidate)

    return FileResponse(root / "index.html")
