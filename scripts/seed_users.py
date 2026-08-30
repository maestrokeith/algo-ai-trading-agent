#!/usr/bin/env python3
"""Seed the TiDB ``users`` table from ``config/users.yaml``.

Usage::

    ALGOSPHERE_LOCAL_SQLITE=1 python scripts/seed_users.py

    TIDB_DSN="mysql+pymysql://..." python scripts/seed_users.py

When ``users.yaml`` does not exist the script creates a single default
admin user (email prompts on stdin) for initial setup.

Idempotent — runs upsert so re-running is safe.
"""
from __future__ import annotations

import argparse
import getpass
import logging
import sys
from pathlib import Path

# Allow running from repo root without installing the package
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.auth import hash_password
from src.db import Base, engine, get_session
from src.db.models import UserRole
from src.db.repos import user_repo
from src.user_manager import load_users

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def _prompt_admin() -> tuple[str, str, str]:
    """Prompt for admin user details when no users.yaml exists."""
    print("No users.yaml found — creating initial admin user.")
    user_id = input("User ID (e.g. 'admin'): ").strip() or "admin"
    email = input("Email: ").strip()
    password = getpass.getpass("Password: ")
    return user_id, email, password


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seed users table from users.yaml")
    parser.add_argument(
        "--admin-email",
        help="Email for the admin user (only used when no users.yaml exists)",
    )
    parser.add_argument(
        "--admin-password",
        help="Password for the admin user (only used when no users.yaml exists)",
    )
    args = parser.parse_args(argv)

    # Ensure tables exist
    Base.metadata.create_all(engine)

    config_dir = Path(__file__).resolve().parents[1] / "config"
    users_yaml = config_dir / "users.yaml"

    seeded = 0

    if users_yaml.exists():
        logger.info("Loading users from %s", users_yaml)
        try:
            user_contexts = load_users(config_path=str(config_dir / "default.yaml"))
        except Exception as exc:
            logger.error("Failed to load users.yaml: %s", exc)
            return 1

        with get_session() as session:
            for ctx in user_contexts:
                user_repo.upsert(
                    session,
                    user_id=ctx.user_id,
                    email=f"{ctx.user_id}@algosphere.local",
                    role=UserRole.trader,
                    paper=ctx.paper,
                    alpaca_key_env=ctx.config.get("alpaca_key_env"),
                    alpaca_secret_env=ctx.config.get("alpaca_secret_env"),
                )
                logger.info("Upserted user: %s (paper=%s)", ctx.user_id, ctx.paper)
                seeded += 1
    else:
        # No users.yaml — create one admin user
        if args.admin_email and args.admin_password:
            user_id, email, password = "admin", args.admin_email, args.admin_password
        else:
            user_id, email, password = _prompt_admin()

        with get_session() as session:
            user_repo.upsert(
                session,
                user_id=user_id,
                email=email,
                hashed_password=hash_password(password),
                role=UserRole.admin,
                paper=True,
            )
            logger.info("Created admin user: %s (%s)", user_id, email)
            seeded += 1

    logger.info("Done — %d user(s) seeded.", seeded)
    return 0


if __name__ == "__main__":
    sys.exit(main())
