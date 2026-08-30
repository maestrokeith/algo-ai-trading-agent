"""
Portfolio & drawdown controls: daily loss limit, max drawdown, safe mode, trade frequency.

- Daily loss limit: ``max_daily_loss_pct`` (positive, e.g. 3.0) or legacy
  ``daily_loss_limit_pct`` (negative threshold) → stop trading for the day when
  ``daily_pnl_pct`` is at or below that threshold.
- Live: when ``live_daily_pnl_from_equity_snapshot`` is true, ``daily_pnl_pct`` is
  taken from broker session return ``(equity - last_equity) / last_equity`` (see
  :meth:`PortfolioRiskManager.refresh_daily_pnl_from_snapshot`). Then
  :meth:`record_trade` / :meth:`record_order_activity` only bump trade counters —
  they do **not** add realized trade return on top, avoiding double-counting with
  mark-to-market already embedded in equity.
- Max drawdown: e.g. -10% → safe mode (paper only) until recovery.
- Trade frequency limit to prevent overtrading loops.
- Per-symbol **entry** caps (live loop): ``max_trades_per_symbol_per_day`` counts each
  submitted buy/sell for that symbol; ``max_round_trips_per_symbol`` counts completed
  long exits (flat). Keys may live under ``portfolio_risk`` or ``risk`` (portfolio wins
  when both set). Counters persist in ``data/portfolio_risk_daily_{user}_{YYYY-MM-DD}.json``.

Multi-user support: ``MultiUserPortfolioRiskManager`` holds per-user
``PortfolioRiskState`` and per-user ``PortfolioRiskManager`` (each user
may have different config overrides).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _daily_risk_counters_path(user_id: str, data_dir: Path, today: date) -> Path:
    return Path(data_dir) / f"portfolio_risk_daily_{user_id}_{today.isoformat()}.json"


@dataclass
class PortfolioRiskState:
    equity_curve: list[tuple[datetime, float]] = field(default_factory=list)
    peak_equity: float = 0.0
    daily_pnl_pct: float = 0.0
    daily_trade_count: int = 0
    daily_trades_per_symbol: dict[str, int] = field(default_factory=dict)
    daily_round_trips_per_symbol: dict[str, int] = field(default_factory=dict)
    last_trade_date: date | None = None
    safe_mode: bool = False
    trading_stopped_for_day: bool = False
    # Live loop: persist per-symbol order + round-trip counts across process restarts (set by run_alpaca_loop).
    risk_counters_user_id: str | None = None
    risk_counters_data_dir: Path | str | None = None
    risk_counters_disk_merge_date: date | None = None

    @property
    def disable_trading(self) -> bool:
        """True after daily loss limit trips (``trading_stopped_for_day``)."""
        return self.trading_stopped_for_day


class PortfolioRiskManager:
    def __init__(self, config: dict[str, Any]):
        pr = config.get("portfolio_risk", {}) or {}
        risk = config.get("risk", {}) or {}
        # max_daily_loss_pct: positive magnitude (e.g. 3.0 → stop when daily_pnl_pct <= -3.0).
        # Takes precedence over legacy daily_loss_limit_pct (negative threshold).
        if "max_daily_loss_pct" in pr and pr.get("max_daily_loss_pct") is not None:
            mag = float(pr.get("max_daily_loss_pct"))
            self.daily_loss_limit_pct = -abs(mag)
        else:
            self.daily_loss_limit_pct = float(pr.get("daily_loss_limit_pct", -2.0))
        self.max_drawdown_pct = float(pr.get("max_drawdown_pct", -10.0))
        self.safe_mode_after_max_dd = bool(pr.get("safe_mode_after_max_dd", True))
        self.recovery_criteria_pct = float(pr.get("recovery_criteria_pct", -8.0))
        self.max_trades_per_day = int(pr.get("max_trades_per_day", 15))
        _raw_mtps = pr.get("max_trades_per_symbol_per_day")
        if _raw_mtps is None or str(_raw_mtps).strip() == "":
            _raw_mtps = risk.get("max_trades_per_symbol_per_day", 3)
        try:
            self.max_trades_per_symbol_per_day = int(_raw_mtps) if _raw_mtps is not None else 3
        except (TypeError, ValueError):
            self.max_trades_per_symbol_per_day = 3
        _raw_mrt = pr.get("max_round_trips_per_symbol")
        if _raw_mrt is None or str(_raw_mrt).strip() == "":
            _raw_mrt = risk.get("max_round_trips_per_symbol", 0)
        try:
            self.max_round_trips_per_symbol = int(_raw_mrt) if _raw_mrt is not None else 0
        except (TypeError, ValueError):
            self.max_round_trips_per_symbol = 0
        # When True, daily_pnl_pct follows (equity - last_equity) / last_equity; record_trade must not += pnl.
        self.live_daily_pnl_from_equity_snapshot = bool(
            pr.get("live_daily_pnl_from_equity_snapshot", False)
        )

    def update_equity(self, state: PortfolioRiskState, dt: datetime, equity: float) -> None:
        state.equity_curve.append((dt, equity))
        if equity > state.peak_equity:
            state.peak_equity = equity

    def current_drawdown_pct(self, state: PortfolioRiskState, current_equity: float) -> float:
        if state.peak_equity <= 0:
            return 0.0
        return ((current_equity - state.peak_equity) / state.peak_equity) * 100.0

    def drawdown_size_multiplier(self, state: PortfolioRiskState, current_equity: float) -> float:
        dd = self.current_drawdown_pct(state, current_equity)
        if dd <= -8.0:
            return 0.50
        if dd <= -5.0:
            return 0.70
        if dd <= -3.0:
            return 0.85
        return 1.0

    def check_daily_reset(self, state: PortfolioRiskState, today: date) -> None:
        if state.last_trade_date != today:
            state.daily_pnl_pct = 0.0
            state.daily_trade_count = 0
            state.daily_trades_per_symbol = {}
            state.daily_round_trips_per_symbol = {}
            state.trading_stopped_for_day = False
            state.last_trade_date = today
            state.risk_counters_disk_merge_date = None
        self._merge_risk_counters_from_disk(state, today)

    def _merge_risk_counters_from_disk(self, state: PortfolioRiskState, today: date) -> None:
        """Load persisted per-symbol counters once per ET day (live loop restarts)."""
        uid = state.risk_counters_user_id
        base = state.risk_counters_data_dir
        if not uid or base is None:
            state.risk_counters_disk_merge_date = today
            return
        if state.risk_counters_disk_merge_date == today:
            return
        p = _daily_risk_counters_path(str(uid), Path(base), today)
        if p.is_file():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                logger.warning("Ignored unreadable portfolio risk counters file: %s", p, exc_info=True)
            else:
                if data.get("et_date") == today.isoformat():
                    try:
                        state.daily_trade_count = max(
                            state.daily_trade_count, int(data.get("daily_trade_count", 0))
                        )
                    except (TypeError, ValueError):
                        pass
                    dts = data.get("daily_trades_per_symbol") or {}
                    if isinstance(dts, dict):
                        for k, v in dts.items():
                            ku = str(k).strip().upper()
                            if not ku:
                                continue
                            try:
                                vi = int(v)
                            except (TypeError, ValueError):
                                continue
                            state.daily_trades_per_symbol[ku] = max(
                                state.daily_trades_per_symbol.get(ku, 0), vi
                            )
                    drt = data.get("daily_round_trips_per_symbol") or {}
                    if isinstance(drt, dict):
                        for k, v in drt.items():
                            ku = str(k).strip().upper()
                            if not ku:
                                continue
                            try:
                                vi = int(v)
                            except (TypeError, ValueError):
                                continue
                            state.daily_round_trips_per_symbol[ku] = max(
                                state.daily_round_trips_per_symbol.get(ku, 0), vi
                            )
        state.risk_counters_disk_merge_date = today

    def persist_daily_risk_counters(self, state: PortfolioRiskState, today: date) -> None:
        uid = state.risk_counters_user_id
        base = state.risk_counters_data_dir
        if not uid or base is None:
            return
        p = _daily_risk_counters_path(str(uid), Path(base), today)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "et_date": today.isoformat(),
                "daily_trade_count": int(state.daily_trade_count),
                "daily_trades_per_symbol": dict(state.daily_trades_per_symbol),
                "daily_round_trips_per_symbol": dict(state.daily_round_trips_per_symbol),
            }
            p.write_text(json.dumps(payload, indent=0), encoding="utf-8")
        except Exception:
            logger.warning("Could not persist portfolio risk counters to %s", p, exc_info=True)

    def refresh_daily_pnl_from_snapshot(
        self,
        state: PortfolioRiskState,
        equity: float,
        last_equity: float | None,
        *,
        today: date | None = None,
    ) -> None:
        """
        When ``live_daily_pnl_from_equity_snapshot`` is enabled, set ``daily_pnl_pct``
        from session mark-to-market vs prior close equity (Alpaca ``last_equity``).

        Call after :meth:`check_daily_reset` for the session calendar day (pass
        ``today`` so rollover zeroing runs first). No-op when the flag is off or
        ``last_equity`` is missing or non-positive.
        """
        if not self.live_daily_pnl_from_equity_snapshot:
            return
        if today is not None:
            self.check_daily_reset(state, today)
        if last_equity is None:
            return
        le = float(last_equity)
        if le <= 0:
            return
        state.daily_pnl_pct = ((float(equity) - le) / le) * 100.0

    def can_trade(
        self,
        state: PortfolioRiskState,
        current_equity: float,
        symbol: str | None,
        today: date | None = None,
        *,
        session_last_equity: float | None = None,
    ) -> tuple[bool, str]:
        """
        Returns (allowed, reason). If not allowed, reason explains why.

        When ``live_daily_pnl_from_equity_snapshot`` is on and ``session_last_equity``
        is provided, ``daily_pnl_pct`` is refreshed from current equity vs prior close
        before evaluating the daily loss gate.
        """
        if today is None:
            today = date.today()
        self.check_daily_reset(state, today)
        self.refresh_daily_pnl_from_snapshot(
            state, current_equity, session_last_equity, today=None
        )

        if state.safe_mode:
            dd = self.current_drawdown_pct(state, current_equity)
            if dd <= self.recovery_criteria_pct:
                return False, f"safe_mode: drawdown {dd:.2f}% not yet recovered to {self.recovery_criteria_pct}%"
            # Optionally clear safe_mode when recovered (implementation can flip state.safe_mode here)

        if state.trading_stopped_for_day:
            return False, "daily loss limit hit; trading stopped for the day"

        # daily_pnl_pct is cumulative % for the day; threshold is negative (e.g. -3.0).
        if state.daily_pnl_pct <= self.daily_loss_limit_pct:
            state.trading_stopped_for_day = True
            return False, (
                f"daily loss limit {self.daily_loss_limit_pct}% hit "
                f"(daily_pnl_pct={state.daily_pnl_pct:.2f}%; disable_trading=True)"
            )

        dd_pct = self.current_drawdown_pct(state, current_equity)
        if dd_pct <= self.max_drawdown_pct and self.safe_mode_after_max_dd:
            state.safe_mode = True
            return False, f"max drawdown {self.max_drawdown_pct}% hit; entering safe mode"

        if state.daily_trade_count >= self.max_trades_per_day:
            return False, f"max trades per day ({self.max_trades_per_day}) reached"

        return True, "ok"

    def can_enter_symbol_activity(
        self, state: PortfolioRiskState, symbol: str, today: date | None = None
    ) -> tuple[bool, str]:
        """Block **new entries** when per-symbol daily order or round-trip caps are hit (live-persisted)."""
        if today is None:
            today = date.today()
        self.check_daily_reset(state, today)
        sym = str(symbol or "").strip().upper()
        if not sym:
            return True, "ok"
        cap_o = int(self.max_trades_per_symbol_per_day)
        if cap_o > 0:
            n = int(state.daily_trades_per_symbol.get(sym, 0))
            if n >= cap_o:
                return False, f"max trades per symbol per day ({cap_o}) for {sym}"
        cap_rt = int(self.max_round_trips_per_symbol)
        if cap_rt > 0:
            r = int(state.daily_round_trips_per_symbol.get(sym, 0))
            if r >= cap_rt:
                return False, f"max round trips per symbol ({cap_rt}) for {sym}"
        return True, "ok"

    def record_trade(
        self, state: PortfolioRiskState, symbol: str, pnl_pct: float | None = None
    ) -> None:
        sym = str(symbol or "").strip().upper()
        if not sym:
            return
        state.daily_trade_count += 1
        state.daily_trades_per_symbol[sym] = state.daily_trades_per_symbol.get(sym, 0) + 1
        if self.live_daily_pnl_from_equity_snapshot:
            return
        if pnl_pct is not None:
            state.daily_pnl_pct += pnl_pct

    def record_order_activity(
        self, state: PortfolioRiskState, symbol: str, realized_pnl_pct: float | None = None
    ) -> None:
        """Same as :meth:`record_trade` — name reflects exit/fill logging at call sites."""
        self.record_trade(state, symbol, realized_pnl_pct)


def note_live_order_for_daily_risk(
    engine: Any,
    symbol: str,
    today: date,
    *,
    side: str,
    full_exit: bool = False,
    pnl_pct: float | None = None,
) -> None:
    """After a live **buy** or **sell** submit, bump persisted daily counters (orders + optional round trip).

    * ``full_exit`` — position is flat after this sell (counts toward ``max_round_trips_per_symbol``).
    * ``side`` — reserved for future asymmetry; both sides count toward ``max_trades_per_symbol_per_day``.
    """
    _ = side
    mgr: PortfolioRiskManager = engine.portfolio_risk
    state: PortfolioRiskState = engine.state.portfolio_risk
    mgr.check_daily_reset(state, today)
    sym = str(symbol).strip().upper()
    if not sym:
        return
    mgr.record_trade(state, sym, pnl_pct)
    if full_exit and mgr.max_round_trips_per_symbol > 0:
        state.daily_round_trips_per_symbol[sym] = state.daily_round_trips_per_symbol.get(sym, 0) + 1
    mgr.persist_daily_risk_counters(state, today)


# ---------------------------------------------------------------------------
# Multi-user wrapper
# ---------------------------------------------------------------------------

class MultiUserPortfolioRiskManager:
    """Per-user portfolio risk management.

    Each user gets their own ``PortfolioRiskState`` and their own
    ``PortfolioRiskManager`` (so per-user config overrides like different
    daily loss limits are respected).

    Parameters
    ----------
    user_configs : dict[str, dict]
        Mapping of ``user_id`` → fully-merged config dict.  Typically
        built by ``UserManager.list_users()`` contexts.
    """

    def __init__(self, user_configs: dict[str, dict[str, Any]] | None = None) -> None:
        self._managers: dict[str, PortfolioRiskManager] = {}
        self._states: dict[str, PortfolioRiskState] = {}
        if user_configs:
            for uid, cfg in user_configs.items():
                self.register_user(uid, cfg)

    def register_user(self, user_id: str, config: dict[str, Any]) -> None:
        """Register a user with their config. Idempotent."""
        if user_id not in self._managers:
            self._managers[user_id] = PortfolioRiskManager(config)
            self._states[user_id] = PortfolioRiskState()
            logger.debug("[%s] Portfolio risk state initialised", user_id)

    def _get(self, user_id: str) -> tuple[PortfolioRiskManager, PortfolioRiskState]:
        try:
            return self._managers[user_id], self._states[user_id]
        except KeyError:
            raise KeyError(
                f"User '{user_id}' not registered with MultiUserPortfolioRiskManager"
            ) from None

    def get_state(self, user_id: str) -> PortfolioRiskState:
        """Return the raw state for *user_id* (read-only inspection)."""
        _, state = self._get(user_id)
        return state

    def update_equity(self, user_id: str, dt: datetime, equity: float) -> None:
        mgr, state = self._get(user_id)
        mgr.update_equity(state, dt, equity)

    def can_trade(
        self,
        user_id: str,
        current_equity: float,
        symbol: str | None,
        today: date | None = None,
        *,
        session_last_equity: float | None = None,
    ) -> tuple[bool, str]:
        mgr, state = self._get(user_id)
        return mgr.can_trade(
            state, current_equity, symbol, today, session_last_equity=session_last_equity
        )

    def can_enter_symbol_activity(
        self, user_id: str, symbol: str, today: date | None = None
    ) -> tuple[bool, str]:
        mgr, state = self._get(user_id)
        return mgr.can_enter_symbol_activity(state, symbol, today)

    def record_trade(
        self, user_id: str, symbol: str, pnl_pct: float | None = None
    ) -> None:
        mgr, state = self._get(user_id)
        mgr.record_trade(state, symbol, pnl_pct)

    def record_order_activity(
        self, user_id: str, symbol: str, realized_pnl_pct: float | None = None
    ) -> None:
        mgr, state = self._get(user_id)
        mgr.record_order_activity(state, symbol, realized_pnl_pct)

    def refresh_daily_pnl_from_snapshot(
        self,
        user_id: str,
        equity: float,
        last_equity: float | None,
        *,
        today: date | None = None,
    ) -> None:
        mgr, state = self._get(user_id)
        mgr.refresh_daily_pnl_from_snapshot(state, equity, last_equity, today=today)

    def check_daily_reset(self, user_id: str, today: date) -> None:
        mgr, state = self._get(user_id)
        mgr.check_daily_reset(state, today)
