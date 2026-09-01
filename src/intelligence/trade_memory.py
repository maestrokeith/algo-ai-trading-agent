"""SQLite-backed structured trade memory."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from .schemas import CriticAssessment, ExecutionResult, PolicyDecision, PostTradeReview, RiskAssessment, StrategyStats, TradeProposal


class TradeMemory:
    def __init__(self, path: str | Path = "data/algo_memory.db") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS trade_proposals (
                    proposal_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    regime TEXT,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS critic_reviews (
                    proposal_id TEXT PRIMARY KEY,
                    approved INTEGER NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS risk_decisions (
                    proposal_id TEXT PRIMARY KEY,
                    approved INTEGER NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS executions (
                    proposal_id TEXT,
                    status TEXT NOT NULL,
                    broker_order_id TEXT,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS closed_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT,
                    strategy TEXT,
                    regime TEXT,
                    return_pct REAL,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS post_trade_reviews (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    strategy TEXT NOT NULL,
                    regime TEXT NOT NULL,
                    return_pct REAL NOT NULL,
                    lesson TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS strategy_statistics (
                    strategy TEXT NOT NULL,
                    regime TEXT NOT NULL,
                    trades INTEGER NOT NULL,
                    win_rate REAL,
                    avg_return REAL,
                    lessons TEXT NOT NULL DEFAULT '[]',
                    PRIMARY KEY(strategy, regime)
                );
                """
            )

    def record_proposal(self, proposal: TradeProposal, regime: str | None = None) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO trade_proposals VALUES (?, ?, ?, ?, ?, ?)",
                (
                    proposal.proposal_id,
                    proposal.symbol,
                    proposal.strategy,
                    regime,
                    _json(proposal),
                    proposal.timestamp.isoformat(),
                ),
            )

    def record_critic(self, review: CriticAssessment) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO critic_reviews VALUES (?, ?, ?)",
                (review.proposal_id, int(review.approved), _json(review)),
            )

    def record_risk(self, risk: RiskAssessment | PolicyDecision) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO risk_decisions VALUES (?, ?, ?)",
                (risk.proposal_id, int(risk.approved), _json(risk)),
            )

    def record_execution(self, execution: ExecutionResult) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO executions VALUES (?, ?, ?, ?)",
                (execution.proposal_id, execution.status.value, execution.broker_order_id, _json(execution)),
            )

    def record_post_trade_review(self, review: PostTradeReview) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO post_trade_reviews(strategy, regime, return_pct, lesson, payload) VALUES (?, ?, ?, ?, ?)",
                (review.strategy, review.regime_at_entry, review.return_pct, review.lesson, _json(review)),
            )
            self._refresh_stats(conn, review.strategy, review.regime_at_entry)

    def strategy_stats(self) -> list[StrategyStats]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM strategy_statistics ORDER BY strategy, regime").fetchall()
        return [
            StrategyStats(
                strategy=row["strategy"],
                regime=row["regime"],
                trades=int(row["trades"]),
                win_rate=row["win_rate"],
                avg_return=row["avg_return"],
                lessons=tuple(json.loads(row["lessons"] or "[]")),
            )
            for row in rows
        ]

    def _refresh_stats(self, conn: sqlite3.Connection, strategy: str, regime: str) -> None:
        rows = conn.execute(
            "SELECT return_pct, lesson FROM post_trade_reviews WHERE strategy=? AND regime=?",
            (strategy, regime),
        ).fetchall()
        trades = len(rows)
        wins = sum(1 for row in rows if float(row["return_pct"]) > 0)
        avg = sum(float(row["return_pct"]) for row in rows) / trades if trades else None
        lessons = [str(row["lesson"]) for row in rows[-5:]]
        conn.execute(
            "INSERT OR REPLACE INTO strategy_statistics VALUES (?, ?, ?, ?, ?, ?)",
            (strategy, regime, trades, wins / trades if trades else None, avg, json.dumps(lessons)),
        )


def _json(obj: Any) -> str:
    def default(value: Any) -> Any:
        if is_dataclass(value):
            return asdict(value)
        if hasattr(value, "isoformat"):
            return value.isoformat()
        if hasattr(value, "value"):
            return value.value
        return repr(value)

    return json.dumps(obj, default=default, sort_keys=True)
