"""Lightweight best-effort SQLite event store for live trading telemetry."""

from __future__ import annotations

import atexit
import json
import logging
import queue
import sqlite3
import threading
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping

log = logging.getLogger(__name__)


def _utc_ts() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _clean_float(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out:
        return None
    return out


def _clean_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _json_dumps(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    except Exception:
        return "{}"


def _cfg_enabled(config: Mapping[str, Any] | None) -> bool:
    db = (config or {}).get("database") if isinstance(config, Mapping) else None
    if not isinstance(db, Mapping):
        return False
    return bool(db.get("enabled", False)) and str(db.get("type", "sqlite")).lower() == "sqlite"


class SQLiteEventStore:
    """Bounded-queue SQLite writer.

    Live trading code should call ``record_*`` methods freely. Calls enqueue rows with
    ``put_nowait`` and drop on overload; all database work runs on a daemon thread.
    """

    def __init__(
        self,
        config: Mapping[str, Any] | None,
        *,
        root: Path | str | None = None,
        max_queue_size: int = 1000,
    ) -> None:
        self.enabled = _cfg_enabled(config)
        self._root = Path(root) if root is not None else Path(__file__).resolve().parents[1]
        self._queue: queue.Queue[tuple[str, tuple[Any, ...]] | None] = queue.Queue(
            maxsize=max_queue_size
        )
        self._conn: sqlite3.Connection | None = None
        self._thread: threading.Thread | None = None
        self._closed = False
        self._last_snapshot_at: dict[str, float] = {}
        if not self.enabled:
            return

        db_cfg = (config or {}).get("database") or {}
        raw_path = str(db_cfg.get("path") or "data/algo_live.db")
        self.path = Path(raw_path)
        if not self.path.is_absolute():
            self.path = self._root / self.path
        self.journal_mode = str(db_cfg.get("journal_mode") or "WAL").upper()
        self.synchronous = str(db_cfg.get("synchronous") or "NORMAL").upper()
        self.busy_timeout_ms = int(db_cfg.get("busy_timeout_ms") or 5000)

        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(
                self.path,
                timeout=max(self.busy_timeout_ms / 1000.0, 0.1),
                check_same_thread=False,
            )
            self._conn.execute(f"PRAGMA journal_mode={self.journal_mode}")
            self._conn.execute(f"PRAGMA synchronous={self.synchronous}")
            self._conn.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
            self._init_schema()
            self._thread = threading.Thread(
                target=self._writer_loop,
                name="sqlite-event-store",
                daemon=True,
            )
            self._thread.start()
        except Exception as exc:
            self.enabled = False
            log.warning("SQLite event store disabled: %s", exc)
            try:
                if self._conn is not None:
                    self._conn.close()
            except Exception:
                pass
            self._conn = None

    def _init_schema(self) -> None:
        if self._conn is None:
            return
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS trades (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              ts TEXT NOT NULL,
              user_id TEXT,
              symbol TEXT,
              side TEXT,
              qty REAL,
              notional REAL,
              price REAL,
              order_id TEXT,
              status TEXT,
              reason TEXT,
              payload_json TEXT
            );
            CREATE TABLE IF NOT EXISTS signals (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              ts TEXT NOT NULL,
              user_id TEXT,
              symbol TEXT,
              signal_type TEXT,
              strength REAL,
              decision TEXT,
              reason TEXT,
              payload_json TEXT
            );
            CREATE TABLE IF NOT EXISTS entry_evaluations (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              ts TEXT NOT NULL,
              user_id TEXT,
              symbol TEXT,
              route TEXT,
              final INTEGER,
              reason TEXT,
              payload_json TEXT
            );
            CREATE TABLE IF NOT EXISTS entry_terminal_outcomes (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              ts TEXT NOT NULL,
              user_id TEXT,
              symbol TEXT,
              route TEXT,
              stage TEXT,
              reason TEXT,
              terminal INTEGER,
              payload_json TEXT
            );
            CREATE TABLE IF NOT EXISTS dynamic_scans (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              ts TEXT NOT NULL,
              user_id TEXT,
              selected_json TEXT,
              candidates_json TEXT,
              payload_json TEXT
            );
            CREATE TABLE IF NOT EXISTS portfolio_snapshots (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              ts TEXT NOT NULL,
              user_id TEXT,
              equity REAL,
              cash REAL,
              buying_power REAL,
              gross_exposure_pct REAL,
              net_exposure_pct REAL,
              positions_count INTEGER,
              payload_json TEXT
            );
            CREATE TABLE IF NOT EXISTS daily_performance (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              trading_date TEXT NOT NULL,
              user_id TEXT,
              equity REAL,
              pnl REAL,
              pnl_pct REAL,
              trades_count INTEGER,
              payload_json TEXT,
              UNIQUE(trading_date, user_id)
            );
            CREATE TABLE IF NOT EXISTS catalyst_outcomes (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              observed_date TEXT NOT NULL,
              ts TEXT NOT NULL,
              user_id TEXT,
              symbol TEXT,
              catalyst_type TEXT,
              news_score REAL,
              subsequent_return_pct REAL,
              source TEXT,
              trade_id TEXT,
              payload_json TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_trades_user_ts ON trades(user_id, ts);
            CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
            CREATE INDEX IF NOT EXISTS idx_signals_user_ts ON signals(user_id, ts);
            CREATE INDEX IF NOT EXISTS idx_signals_symbol ON signals(symbol);
            CREATE INDEX IF NOT EXISTS idx_entry_evaluations_user_ts ON entry_evaluations(user_id, ts);
            CREATE INDEX IF NOT EXISTS idx_entry_evaluations_symbol ON entry_evaluations(symbol);
            CREATE INDEX IF NOT EXISTS idx_entry_terminal_outcomes_user_ts ON entry_terminal_outcomes(user_id, ts);
            CREATE INDEX IF NOT EXISTS idx_entry_terminal_outcomes_symbol ON entry_terminal_outcomes(symbol);
            CREATE INDEX IF NOT EXISTS idx_dynamic_scans_user_ts ON dynamic_scans(user_id, ts);
            CREATE INDEX IF NOT EXISTS idx_portfolio_snapshots_user_ts ON portfolio_snapshots(user_id, ts);
            CREATE INDEX IF NOT EXISTS idx_daily_performance_user_date ON daily_performance(user_id, trading_date);
            CREATE INDEX IF NOT EXISTS idx_catalyst_outcomes_user_date ON catalyst_outcomes(user_id, observed_date);
            CREATE INDEX IF NOT EXISTS idx_catalyst_outcomes_type ON catalyst_outcomes(catalyst_type);
            """
        )
        self._conn.commit()

    def _writer_loop(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                self._queue.task_done()
                break
            sql, params = item
            try:
                if self._conn is not None:
                    self._conn.execute(sql, params)
                    self._conn.commit()
            except Exception as exc:
                log.debug("SQLite event write dropped: %s", exc)
            finally:
                self._queue.task_done()

    def _enqueue(self, sql: str, params: tuple[Any, ...]) -> None:
        if not self.enabled or self._closed:
            return
        try:
            self._queue.put_nowait((sql, params))
        except queue.Full:
            log.debug("SQLite event queue full; dropping row")
        except Exception as exc:
            log.debug("SQLite enqueue failed: %s", exc)

    def flush(self, timeout: float = 2.0) -> None:
        if not self.enabled:
            return
        done = threading.Event()

        def waiter() -> None:
            try:
                self._queue.join()
            finally:
                done.set()

        threading.Thread(target=waiter, daemon=True).start()
        done.wait(timeout=max(timeout, 0.0))

    def close(self, timeout: float = 2.0) -> None:
        if self._closed:
            return
        self._closed = True
        if self.enabled:
            self.flush(timeout=timeout)
            try:
                self._queue.put_nowait(None)
            except Exception:
                pass
            if self._thread is not None:
                self._thread.join(timeout=timeout)
        try:
            if self._conn is not None:
                self._conn.close()
        except Exception:
            pass

    def record_trade(
        self,
        *,
        user_id: str | None,
        symbol: str,
        side: str,
        qty: Any = None,
        notional: Any = None,
        price: Any = None,
        order_id: str | None = None,
        status: str | None = None,
        reason: str | None = None,
        payload: Mapping[str, Any] | None = None,
        ts: str | None = None,
    ) -> None:
        self._enqueue(
            """
            INSERT INTO trades
            (ts, user_id, symbol, side, qty, notional, price, order_id, status, reason, payload_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ts or _utc_ts(),
                user_id,
                str(symbol or "").upper(),
                str(side or "").lower(),
                _clean_float(qty),
                _clean_float(notional),
                _clean_float(price),
                order_id,
                status,
                reason,
                _json_dumps(payload or {}),
            ),
        )

    def record_signal(
        self,
        *,
        user_id: str | None,
        symbol: str,
        signal_type: str,
        strength: Any = None,
        decision: str | None = None,
        reason: str | None = None,
        payload: Mapping[str, Any] | None = None,
        ts: str | None = None,
    ) -> None:
        self._enqueue(
            """
            INSERT INTO signals
            (ts, user_id, symbol, signal_type, strength, decision, reason, payload_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ts or _utc_ts(),
                user_id,
                str(symbol or "").upper(),
                signal_type,
                _clean_float(strength),
                decision,
                reason,
                _json_dumps(payload or {}),
            ),
        )

    def record_entry_evaluation(
        self,
        *,
        user_id: str | None,
        symbol: str,
        route: str,
        final: bool,
        reason: str | None,
        payload: Mapping[str, Any] | None = None,
        ts: str | None = None,
    ) -> None:
        self._enqueue(
            """
            INSERT INTO entry_evaluations
            (ts, user_id, symbol, route, final, reason, payload_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ts or _utc_ts(),
                user_id,
                str(symbol or "").upper(),
                route,
                1 if final else 0,
                reason,
                _json_dumps(payload or {}),
            ),
        )

    def record_dynamic_scan(
        self,
        *,
        user_id: str | None,
        selected: list[str] | tuple[str, ...] | None,
        candidates: Any = None,
        payload: Mapping[str, Any] | None = None,
        ts: str | None = None,
    ) -> None:
        self._enqueue(
            """
            INSERT INTO dynamic_scans
            (ts, user_id, selected_json, candidates_json, payload_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                ts or _utc_ts(),
                user_id,
                _json_dumps(list(selected or [])),
                _json_dumps(candidates or []),
                _json_dumps(payload or {}),
            ),
        )

    def record_entry_terminal_outcome(
        self,
        *,
        user_id: str | None,
        symbol: str,
        route: str | None,
        stage: str,
        reason: str | None,
        terminal: bool = True,
        payload: Mapping[str, Any] | None = None,
        ts: str | None = None,
    ) -> None:
        self._enqueue(
            """
            INSERT INTO entry_terminal_outcomes
            (ts, user_id, symbol, route, stage, reason, terminal, payload_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ts or _utc_ts(),
                user_id,
                str(symbol or "").upper(),
                route,
                stage,
                reason,
                1 if terminal else 0,
                _json_dumps(payload or {}),
            ),
        )

    def record_portfolio_snapshot(
        self,
        *,
        user_id: str | None,
        equity: Any,
        cash: Any = None,
        buying_power: Any = None,
        gross_exposure_pct: Any = None,
        net_exposure_pct: Any = None,
        positions_count: Any = None,
        payload: Mapping[str, Any] | None = None,
        min_interval_seconds: float | None = None,
        ts: str | None = None,
    ) -> None:
        key = str(user_id or "default")
        if min_interval_seconds is not None and min_interval_seconds > 0:
            now = datetime.now(timezone.utc).timestamp()
            last = self._last_snapshot_at.get(key, 0.0)
            if now - last < min_interval_seconds:
                return
            self._last_snapshot_at[key] = now
        self._enqueue(
            """
            INSERT INTO portfolio_snapshots
            (ts, user_id, equity, cash, buying_power, gross_exposure_pct, net_exposure_pct, positions_count, payload_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ts or _utc_ts(),
                user_id,
                _clean_float(equity),
                _clean_float(cash),
                _clean_float(buying_power),
                _clean_float(gross_exposure_pct),
                _clean_float(net_exposure_pct),
                _clean_int(positions_count),
                _json_dumps(payload or {}),
            ),
        )

    def record_daily_performance(
        self,
        *,
        user_id: str | None,
        trading_date: date | str,
        equity: Any = None,
        pnl: Any = None,
        pnl_pct: Any = None,
        trades_count: Any = None,
        payload: Mapping[str, Any] | None = None,
    ) -> None:
        self._enqueue(
            """
            INSERT INTO daily_performance
            (trading_date, user_id, equity, pnl, pnl_pct, trades_count, payload_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(trading_date, user_id) DO UPDATE SET
              equity=excluded.equity,
              pnl=excluded.pnl,
              pnl_pct=excluded.pnl_pct,
              trades_count=excluded.trades_count,
              payload_json=excluded.payload_json
            """,
            (
                str(trading_date),
                user_id,
                _clean_float(equity),
                _clean_float(pnl),
                _clean_float(pnl_pct),
                _clean_int(trades_count),
                _json_dumps(payload or {}),
            ),
        )

    def record_catalyst_outcome(
        self,
        *,
        user_id: str | None,
        symbol: str,
        catalyst_type: str,
        news_score: Any,
        subsequent_return_pct: Any,
        observed_date: date | str,
        source: str | None = None,
        trade_id: str | None = None,
        payload: Mapping[str, Any] | None = None,
        ts: str | None = None,
    ) -> None:
        self._enqueue(
            """
            INSERT INTO catalyst_outcomes
            (observed_date, ts, user_id, symbol, catalyst_type, news_score, subsequent_return_pct, source, trade_id, payload_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(observed_date),
                ts or _utc_ts(),
                user_id,
                str(symbol or "").upper(),
                str(catalyst_type or "unknown").lower(),
                _clean_float(news_score),
                _clean_float(subsequent_return_pct),
                source,
                trade_id,
                _json_dumps(payload or {}),
            ),
        )


_STORE_LOCK = threading.Lock()
_STORE: SQLiteEventStore | None = None


def get_sqlite_event_store(config: Mapping[str, Any] | None) -> SQLiteEventStore:
    """Return the process-level live SQLite event store."""
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            _STORE = SQLiteEventStore(config)
        return _STORE


def _close_global_store() -> None:
    with _STORE_LOCK:
        if _STORE is not None:
            _STORE.close(timeout=2.0)


atexit.register(_close_global_store)
