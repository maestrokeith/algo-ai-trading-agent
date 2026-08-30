#!/usr/bin/env python3
"""Create local SQLite DB (./data/algosphere.db) and run Alembic migrations.

Does not require the ``alembic`` shell on PATH — uses the Alembic Python API.

Usage::

    python scripts/bootstrap_local_db.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("ALGOSPHERE_LOCAL_SQLITE", "1")

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402


def main() -> None:
    os.chdir(PROJECT_ROOT)
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    command.upgrade(cfg, "head")
    print("Migrations applied. Start API with:")
    print('  ALGOSPHERE_LOCAL_SQLITE=1 JWT_SECRET="your-secret" python scripts/run_api.py')


if __name__ == "__main__":
    main()
