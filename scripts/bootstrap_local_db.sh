#!/usr/bin/env bash
# Create local SQLite DB under ./data/ and apply Alembic migrations.
set -euo pipefail
cd "$(dirname "$0")/.."
export ALGOSPHERE_LOCAL_SQLITE=1
export PYTHONPATH=.
mkdir -p data
echo "Applying migrations to local SQLite (data/algosphere.db)..."
python scripts/bootstrap_local_db.py
echo "Done. Start API with:"
echo "  ALGOSPHERE_LOCAL_SQLITE=1 JWT_SECRET=\"your-secret\" python scripts/run_api.py"
echo "Seed users (optional):"
echo "  ALGOSPHERE_LOCAL_SQLITE=1 python scripts/seed_users.py"
