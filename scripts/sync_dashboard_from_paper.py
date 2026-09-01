#!/usr/bin/env python3
"""Sync local paper trading artifacts into the dashboard database.

The live/paper loop writes runtime telemetry to ``data/algo_live.db`` and
position tracker JSON files. The web dashboard reads SQLAlchemy tables from
``data/algosphere.db`` when ``ALGOSPHERE_LOCAL_SQLITE=1`` is enabled. This
script bridges those local-dev stores for the paper user.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("ALGOSPHERE_LOCAL_SQLITE", "1")

from sqlalchemy import delete

from src.auth import hash_password
from src.db import Base, engine, get_session
from src.db.models import PortfolioSnapshot, Trade, UserRole
from src.db.repos import portfolio_repo, user_repo


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _parse_dt(value: Any) -> datetime | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _float_or_none(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out else None


def _sync_snapshots(session, *, user_id: str, event_db: Path, limit: int) -> int:
    if not event_db.exists():
        return 0
    conn = sqlite3.connect(event_db)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT ts, equity, cash, buying_power
            FROM portfolio_snapshots
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (user_id, int(limit)),
        ).fetchall()
    finally:
        conn.close()
    session.execute(delete(PortfolioSnapshot).where(PortfolioSnapshot.user_id == user_id))
    for row in reversed(rows):
        snap = portfolio_repo.snapshot_portfolio(
            session,
            user_id=user_id,
            equity=_float_or_none(row["equity"]),
            cash=_float_or_none(row["cash"]),
            buying_power=_float_or_none(row["buying_power"]),
        )
        parsed = _parse_dt(row["ts"])
        if parsed is not None:
            snap.captured_at = parsed
    return len(rows)


def _sync_positions(session, *, user_id: str, positions_path: Path) -> int:
    portfolio_repo.clear_positions(session, user_id)
    if not positions_path.exists():
        return 0
    try:
        data = json.loads(positions_path.read_text(encoding="utf-8"))
    except Exception:
        return 0
    if not isinstance(data, dict):
        return 0
    count = 0
    for symbol, row in data.items():
        if not isinstance(row, dict):
            continue
        qty = _float_or_none(row.get("qty"))
        if qty is None or qty <= 0:
            continue
        portfolio_repo.upsert_position(
            session,
            user_id=user_id,
            symbol=str(symbol).upper(),
            qty=qty,
            avg_entry_price=_float_or_none(row.get("entry_price")),
            current_price=_float_or_none(row.get("current_price") or row.get("last_price")),
            unrealized_pnl=_float_or_none(row.get("unrealized_pnl")),
            stop_pct=_float_or_none(row.get("stop_pct")),
            partial_taken=bool(row.get("partial_taken", False)),
            trail_high=_float_or_none(row.get("trail_high")),
            entered_at=_parse_dt(row.get("entry_time")),
        )
        count += 1
    return count


def _sync_from_broker(session, *, user_id: str) -> tuple[bool, int]:
    api_key = os.environ.get("APCA_API_KEY_ID", "")
    api_secret = os.environ.get("APCA_API_SECRET_KEY", "")
    if not api_key or not api_secret:
        print("broker_sync=skipped reason=missing_APCA_API_KEY_ID_or_APCA_API_SECRET_KEY")
        return False, 0
    from src.brokers.alpaca_client import AlpacaBroker
    from src.config_loader import load_config

    config = load_config()
    broker = AlpacaBroker(config, api_key=api_key, secret=api_secret, paper=True)
    account = broker.get_account()
    portfolio_repo.snapshot_portfolio(
        session,
        user_id=user_id,
        equity=account.equity,
        cash=account.cash,
        buying_power=account.buying_power,
    )
    portfolio_repo.clear_positions(session, user_id)
    count = 0
    for pos in broker.list_positions():
        portfolio_repo.upsert_position(
            session,
            user_id=user_id,
            symbol=pos.symbol,
            side=pos.side,
            qty=pos.qty,
            avg_entry_price=pos.avg_entry_price,
            current_price=pos.current_price,
            unrealized_pnl=pos.unrealized_pl,
        )
        count += 1
    return True, count


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sync paper_bot data into local dashboard DB")
    parser.add_argument("--user-id", default="paper_bot")
    parser.add_argument("--email", default="paper_bot@algosphere.local")
    parser.add_argument("--password", default="paper-bot-local")
    parser.add_argument("--snapshot-limit", type=int, default=500)
    parser.add_argument("--from-broker", action="store_true", help="Also pull current Alpaca paper account/positions from APCA_* env vars")
    args = parser.parse_args(argv)

    Base.metadata.create_all(engine)
    event_db = PROJECT_ROOT / "data" / "algo_live.db"
    positions_path = PROJECT_ROOT / "data" / f"positions_{args.user_id}.json"

    with get_session() as session:
        user_repo.upsert(
            session,
            user_id=args.user_id,
            email=args.email.lower(),
            hashed_password=hash_password(args.password),
            role=UserRole.trader,
            paper=True,
        )
        snapshots = _sync_snapshots(
            session,
            user_id=args.user_id,
            event_db=event_db,
            limit=args.snapshot_limit,
        )
        if args.from_broker:
            broker_ok, positions = _sync_from_broker(session, user_id=args.user_id)
        else:
            broker_ok = False
            positions = _sync_positions(session, user_id=args.user_id, positions_path=positions_path)
        closed_trades = session.query(Trade).filter(Trade.user_id == args.user_id).count()

    print(
        f"dashboard_sync user_id={args.user_id} email={args.email.lower()} "
        f"snapshots={snapshots} positions={positions} closed_trades={closed_trades} "
        f"broker_sync={str(broker_ok).lower()}"
    )
    print("login_email=%s" % args.email.lower())
    print("login_password=<redacted>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
