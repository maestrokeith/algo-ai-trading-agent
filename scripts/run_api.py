#!/usr/bin/env python3
"""Run the AlgoSphere API server.

Usage (pick one database option)::

    # Local persistent SQLite (creates ./data/algosphere.db, gitignored)
    ALGOSPHERE_LOCAL_SQLITE=1 JWT_SECRET="..." python scripts/run_api.py

    # TiDB / MySQL
    TIDB_DSN="mysql+pymysql://..." JWT_SECRET="..." python scripts/run_api.py

    # Generic
    DATABASE_URL="postgresql+psycopg2://..." JWT_SECRET="..." python scripts/run_api.py

Run ``./scripts/bootstrap_local_db.sh`` once to create the DB file and apply migrations.

Defaults to port 8000. Override with API_PORT env var.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn

if __name__ == "__main__":
    port = int(os.environ.get("API_PORT", "8000"))
    uvicorn.run(
        "src.api.main:app",
        host="0.0.0.0",
        port=port,
        reload=os.environ.get("API_RELOAD", "").lower() in ("1", "true"),
    )
