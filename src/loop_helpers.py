"""Helpers for multi-user trading loop orchestration.

``UserLoopContext`` bundles all per-user runtime state (broker, engine,
risk managers, tracker path) so the main loop can iterate over users
with full isolation.

``init_user_contexts`` creates these contexts from a ``UserManager``,
and ``run_user_pass`` wraps per-user logic with error isolation.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, NamedTuple

from src.compliance import ComplianceManager, PDTState
from src.execution import minutes_since_last_broker_structured_exit, parse_per_symbol_sell_cooldown_min
from src.portfolio_risk import MultiUserPortfolioRiskManager, PortfolioRiskManager, PortfolioRiskState
from src.trading_engine import TradingEngine
from src.user_manager import UserContext, UserManager

logger = logging.getLogger(__name__)


def resolve_live_loop_intervals(config: Mapping[str, Any] | None) -> tuple[int, int]:
    """Exit and entry scan intervals (minutes) for ``run_alpaca_loop``.

    ``timing.exit_interval_min`` / ``timing.entry_interval_min`` override
    ``broker.exit_check_interval_minutes`` (or legacy ``check_interval_minutes``)
    and ``broker.entry_check_interval_minutes``. Minimum enforced: 1 minute.

    Defaults when keys are absent: exit 12, entry 10 (10–15 min exits is a
    common tuning band; 12 is a balanced default).
    """
    cfg = dict(config) if config is not None else {}
    timing_raw = cfg.get("timing")
    timing: Mapping[str, Any] = timing_raw if isinstance(timing_raw, Mapping) else {}
    broker_raw = cfg.get("broker")
    broker_cfg: Mapping[str, Any] = broker_raw if isinstance(broker_raw, Mapping) else {}

    def _coerce_minutes(raw: Any, fallback: int) -> int:
        if raw is None:
            return max(1, fallback)
        if isinstance(raw, str) and not str(raw).strip():
            return max(1, fallback)
        try:
            v = int(raw)
        except (TypeError, ValueError):
            return max(1, fallback)
        return max(1, v)

    raw_exit_timing = timing.get("exit_interval_min")
    if raw_exit_timing is not None and not (isinstance(raw_exit_timing, str) and not str(raw_exit_timing).strip()):
        exit_min = _coerce_minutes(raw_exit_timing, 12)
    else:
        br_exit = broker_cfg.get("exit_check_interval_minutes") or broker_cfg.get("check_interval_minutes")
        exit_min = _coerce_minutes(br_exit, 12)

    raw_ent_timing = timing.get("entry_interval_min")
    if raw_ent_timing is not None and not (isinstance(raw_ent_timing, str) and not str(raw_ent_timing).strip()):
        entry_min = _coerce_minutes(raw_ent_timing, 10)
    else:
        entry_min = _coerce_minutes(broker_cfg.get("entry_check_interval_minutes"), 10)

    return exit_min, entry_min


def resolve_dynamic_momentum_intervals(
    config: Mapping[str, Any] | None,
) -> tuple[int | None, int | None]:
    """
    Entry/exit cadence (minutes) for **scanner-added** symbols only when ``dynamic_universe.enabled``.

    Returns ``(entry_min, exit_min)`` where each value is ``None`` if unset (caller should fall back to
    core :func:`resolve_live_loop_intervals`). When dynamic universe is disabled, returns ``(None, None)``.
    """

    def _coerce_optional_minutes(raw: Any) -> int | None:
        if raw is None or (isinstance(raw, str) and not str(raw).strip()):
            return None
        try:
            return max(1, int(float(raw)))
        except (TypeError, ValueError):
            return None

    cfg = dict(config) if config is not None else {}
    du = cfg.get("dynamic_universe")
    if not isinstance(du, Mapping) or not bool(du.get("enabled", False)):
        return None, None

    ent = _coerce_optional_minutes(du.get("entry_check_interval_minutes"))
    ext = _coerce_optional_minutes(du.get("exit_check_interval_minutes"))
    return ent, ext


def reduce_only_mode_exit_interval_minutes(config: Mapping[str, Any] | None) -> int:
    """
    Exit cadence in minutes when the account is in **reduce_only** (gross over threshold).

    Read ``timing.reduce_only_mode.exit_interval_minutes`` (or ``exit_interval_min``);
    if the map is missing, a top-level ``reduce_only_mode`` is accepted. Default ``5``;
    minimum **1** minute.
    """
    cfg = dict(config) if config is not None else {}
    timing_raw = cfg.get("timing")
    timing: dict[str, Any] = (
        dict(timing_raw) if isinstance(timing_raw, Mapping) else {}
    )
    sub = timing.get("reduce_only_mode")
    if not isinstance(sub, Mapping):
        ro = cfg.get("reduce_only_mode")
        sub = ro if isinstance(ro, Mapping) else {}
    if not sub:
        return 5
    raw = sub.get("exit_interval_minutes", sub.get("exit_interval_min"))
    if raw is None or (isinstance(raw, str) and not str(raw).strip()):
        return 5
    try:
        v = int(float(raw))
    except (TypeError, ValueError):
        return 5
    return max(1, v)


@dataclass
class UserLoopContext:
    """All per-user state needed for one trading-loop iteration."""

    user_id: str
    user_ctx: UserContext
    broker: Any  # AlpacaBroker — typed as Any to avoid import
    engine: TradingEngine
    config: dict[str, Any]
    paper: bool
    data_dir: Path | None = None  # position tracker data dir (None → default)


def init_user_contexts(
    user_manager: UserManager,
    *,
    project_root: Path | None = None,
    user_filter: str | None = None,
) -> list[UserLoopContext]:
    """Create :class:`UserLoopContext` for every user in *user_manager*.

    Parameters
    ----------
    user_manager:
        Loaded ``UserManager`` instance.
    project_root:
        Project root for data dir resolution.  Falls back to
        ``Path(__file__).parent.parent``.
    user_filter:
        If provided, only create context for this ``user_id``.  Raises
        ``KeyError`` if the user is not registered.

    Returns
    -------
    list[UserLoopContext]
        One context per user, in registration order.
    """
    if project_root is None:
        project_root = Path(__file__).resolve().parent.parent

    users = user_manager.list_users()
    if user_filter:
        # Validate the filter early
        user_manager.get_user(user_filter)
        users = [u for u in users if u.user_id == user_filter]

    contexts: list[UserLoopContext] = []
    for uctx in users:
        uid = uctx.user_id
        try:
            broker = user_manager.get_broker(uid)
            engine = TradingEngine(config=uctx.config)
            ctx = UserLoopContext(
                user_id=uid,
                user_ctx=uctx,
                broker=broker,
                engine=engine,
                config=uctx.config,
                paper=uctx.paper,
                data_dir=project_root / "data",
            )
            contexts.append(ctx)
            logger.info(
                "[%s] Initialised loop context (paper=%s)", uid, uctx.paper
            )
        except Exception:
            logger.exception("[%s] Failed to initialise loop context — skipping user", uid)
    return contexts


def log_startup_summary(contexts: list[UserLoopContext]) -> None:
    """Print a human-readable startup summary of loaded users."""
    if not contexts:
        logger.warning("No user contexts loaded — nothing to trade.")
        return
    logger.info("Loaded %d user(s):", len(contexts))
    for ctx in contexts:
        mode = "PAPER" if ctx.paper else "LIVE"
        logger.info("  [%s] %s", ctx.user_id, mode)


def run_user_pass(
    ctx: UserLoopContext,
    callback: Any,
    **kwargs: Any,
) -> bool:
    """Execute *callback(ctx, **kwargs)* with error isolation.

    Returns ``True`` if the callback succeeded, ``False`` if an
    exception was caught (logged and swallowed so the next user
    can proceed).
    """
    try:
        callback(ctx, **kwargs)
        return True
    except Exception:
        logger.exception(
            "[%s] Error during trading pass — skipping to next user",
            ctx.user_id,
        )
        return False


def parse_cli_args(argv: list[str] | None = None) -> Any:
    """Parse CLI arguments for the multi-user trading loop.

    Returns the parsed ``argparse.Namespace``.
    """
    import argparse

    parser = argparse.ArgumentParser(
        description="Run trading loop until market close (multi-user)"
    )
    parser.add_argument(
        "--live", action="store_true",
        help="Use live account (ignored in multi-user mode)",
    )
    parser.add_argument(
        "--paper", action="store_true",
        help="Use paper account (ignored in multi-user mode)",
    )
    parser.add_argument(
        "--mode",
        choices=["live", "paper", "shadow", "entries-disabled"],
        default=None,
        help="Explicit trading-control mode.",
    )
    parser.add_argument(
        "--user",
        type=str,
        default=None,
        help="Run only this user_id (useful for testing)",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Print skip reasons for all symbols",
    )
    return parser.parse_args(argv)


def is_alpaca_pdt_trade_denial(exc: BaseException) -> bool:
    """True if *exc* looks like Alpaca rejecting an order for pattern-day-trader rules."""
    text = str(exc)
    lower = text.lower()
    if "40310100" in text:
        return True
    code = getattr(exc, "code", None)
    if code is not None and str(code) == "40310100":
        return True
    if "pattern day" in lower and ("denied" in lower or "trade" in lower):
        return True
    return False


def alpaca_pdt_exit_hint_line() -> str:
    """Follow-up line for stdout when a protective close hits Alpaca PDT denial."""
    return (
        "Alpaca rejected the close (PDT). Position left open in tracker. "
        "Options: raise equity to $25k+, sell next session, use Alpaca cash-account rules, or close from the Alpaca dashboard per your agreement."
    )


def entries_min_trade_size_dollars(entries_cfg: Mapping[str, Any] | None) -> float:
    """Minimum buying power (USD) to run a new-entry scan; 0 means gate disabled."""
    e = entries_cfg if isinstance(entries_cfg, dict) else {}
    raw = e.get("min_trade_size")
    if raw is None or str(raw).strip() == "":
        return 0.0
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return 0.0


def entries_insufficient_buying_power(
    available_cash: float, entries_cfg: Mapping[str, Any] | None
) -> bool:
    """True when ``entries.min_trade_size`` is set and *available_cash* is below it."""
    m = entries_min_trade_size_dollars(entries_cfg)
    return m > 0 and float(available_cash) < m


def _entries_cancel_orders_older_than_minutes(entries_cfg: Mapping[str, Any] | None) -> float:
    e = entries_cfg if isinstance(entries_cfg, dict) else {}
    raw = e.get("cancel_orders_older_than_minutes")
    if raw is None or str(raw).strip() == "":
        raw = e.get("cancel_stale_open_orders_minutes")
    if raw is None or str(raw).strip() == "":
        return 0.0
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return 0.0


def _parse_order_submitted_at_utc(raw: Any) -> datetime | None:
    """Normalize Alpaca ``submitted_at`` / ``created_at`` to timezone-aware UTC."""
    if raw is None:
        return None
    if isinstance(raw, datetime):
        dt = raw
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    if isinstance(raw, str) and raw.strip():
        s = raw.strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    return None


def cancel_orders_older_than(
    broker: Any,
    *,
    minutes: float,
    now: datetime,
    verbose: bool = False,
) -> int:
    """
    Cancel broker open orders whose ``submitted_at`` is more than *minutes* ago.

    When *minutes* is ``<= 0``, returns ``0`` without calling the broker.
    Brokers without ``get_open_orders`` / ``cancel_order_by_id`` are no-ops.

    Returns
    -------
    int
        Number of orders successfully cancelled.
    """
    stale_min = float(minutes)
    if stale_min <= 0:
        return 0
    get_fn = getattr(broker, "get_open_orders", None)
    cancel_fn = getattr(broker, "cancel_order_by_id", None)
    if get_fn is None or cancel_fn is None:
        return 0
    now_utc = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    now_utc = now_utc.astimezone(timezone.utc)
    orders = get_fn() or []
    cancelled = 0
    threshold = timedelta(minutes=stale_min)
    for o in orders:
        oid = str(o.get("id") or "").strip()
        if not oid:
            continue
        submitted = _parse_order_submitted_at_utc(o.get("submitted_at"))
        if submitted is None:
            continue
        if now_utc - submitted < threshold:
            continue
        try:
            cancel_fn(oid)
            cancelled += 1
            sym = str(o.get("symbol") or "")
            logger.info(
                "Cancelled open order id=%s symbol=%s (submitted %s, older_than %.0f min)",
                oid,
                sym,
                submitted.isoformat(),
                stale_min,
            )
        except Exception:
            logger.warning(
                "Failed to cancel open order id=%s symbol=%s",
                oid,
                o.get("symbol"),
                exc_info=True,
            )
    if verbose and cancelled:
        try:
            et_s = now.strftime("%H:%M ET") if hasattr(now, "strftime") else ""
        except Exception:
            et_s = ""
        print(
            (et_s + " — " if et_s else "")
            + "cancelled %d open order(s) older than %.0f min" % (cancelled, stale_min),
            flush=True,
        )
    return cancelled


def _order_side_str(order: Any) -> str:
    if isinstance(order, dict):
        return str(order.get("side") or "").strip().lower()
    return str(getattr(order, "side", "") or "").strip().lower()


def _order_symbol_str(order: Any) -> str:
    if isinstance(order, dict):
        return str(order.get("symbol") or "").strip().upper()
    return str(getattr(order, "symbol", "") or "").strip().upper()


def _order_id_str(order: Any) -> str:
    if isinstance(order, dict):
        return str(order.get("id") or "").strip()
    return str(getattr(order, "id", "") or "").strip()


def _position_qty_float(position: Any) -> float:
    """Shares / contracts from an Alpaca-style position dict or model (same sign as ``position.qty``)."""
    if isinstance(position, dict):
        try:
            return float(position.get("qty", 0) or 0)
        except (TypeError, ValueError):
            return 0.0
    try:
        return float(getattr(position, "qty", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def broker_available_qty_for_symbol(broker: Any, symbol: str) -> float:
    """
    Current open position quantity for *symbol* (long stock/options rows use positive ``qty``).

    Uses ``get_position(symbol)`` when present; otherwise scans ``get_positions()`` for a matching
    ``symbol``. Returns ``0.0`` when flat or when broker helpers are missing / fail.
    """
    su = str(symbol or "").strip().upper()
    if not su:
        return 0.0
    get_one = getattr(broker, "get_position", None)
    if callable(get_one):
        try:
            pos = get_one(su)
        except Exception:
            logger.warning(
                "broker_available_qty_for_symbol: get_position failed for symbol=%s",
                su,
                exc_info=True,
            )
            pos = None
        if pos is not None:
            return _position_qty_float(pos)
    get_all = getattr(broker, "get_positions", None)
    if not callable(get_all):
        return 0.0
    try:
        rows = get_all() or []
    except Exception:
        logger.warning(
            "broker_available_qty_for_symbol: get_positions failed for symbol=%s",
            su,
            exc_info=True,
        )
        return 0.0
    for row in rows:
        sym = _order_symbol_str(row)
        if sym == su:
            return _position_qty_float(row)
    return 0.0


class EmergencyPrepareResult(NamedTuple):
    """Result of :func:`emergency_prepare_symbol`."""

    cancelled_sell_orders: int
    """Number of open sell orders cancelled for the symbol."""

    available_qty: float
    """Broker-reported position qty after cancels (same units as ``position.qty``)."""


def emergency_prepare_symbol(
    broker: Any,
    symbol: str,
    *,
    sleep_seconds: float = 1.0,
) -> EmergencyPrepareResult:
    """
    Before an emergency sell for *symbol*, cancel open **sell** orders on that symbol so reserved
    quantity is released. Then refresh **available position qty** from the broker (``get_position`` or
    ``get_positions``) so callers can size sells against freed inventory.

    Pulls open orders via ``get_open_orders``, filters by *symbol* and side ``sell``, then
    ``cancel_order_by_id`` for each. Sleeps *sleep_seconds* (default 1) after cancels when at least
    one sell was cancelled.

    When ``get_open_orders`` / ``cancel_order_by_id`` are missing, cancellation is skipped but
    ``available_qty`` is still populated when ``get_position`` / ``get_positions`` exist.
    """
    su = str(symbol or "").strip().upper()
    if not su:
        return EmergencyPrepareResult(0, 0.0)

    cancelled = 0
    get_fn = getattr(broker, "get_open_orders", None)
    cancel_fn = getattr(broker, "cancel_order_by_id", None)
    if get_fn is not None and cancel_fn is not None:
        try:
            raw = get_fn() or []
        except Exception:
            logger.warning(
                "emergency_prepare_symbol: get_open_orders failed for symbol=%s",
                su,
                exc_info=True,
            )
            raw = []
        for o in raw:
            if _order_symbol_str(o) != su:
                continue
            if _order_side_str(o) != "sell":
                continue
            oid = _order_id_str(o)
            if not oid:
                continue
            try:
                cancel_fn(oid)
                cancelled += 1
                logger.info(
                    "emergency_prepare_symbol: cancelled open sell order id=%s symbol=%s",
                    oid,
                    su,
                )
            except Exception:
                logger.warning(
                    "emergency_prepare_symbol: failed to cancel order id=%s symbol=%s",
                    oid,
                    su,
                    exc_info=True,
                )
    if sleep_seconds and sleep_seconds > 0 and cancelled:
        time.sleep(float(sleep_seconds))

    available_qty = broker_available_qty_for_symbol(broker, su)
    return EmergencyPrepareResult(cancelled, available_qty)


def cancel_stale_orders(
    broker: Any,
    *,
    now: datetime,
    entries_cfg: Mapping[str, Any] | None,
    verbose: bool = False,
) -> int:
    """
    Cancel open orders using ``entries.cancel_orders_older_than_minutes`` (or legacy
    ``entries.cancel_stale_open_orders_minutes``). See :func:`cancel_orders_older_than`.
    """
    m = _entries_cancel_orders_older_than_minutes(entries_cfg)
    return cancel_orders_older_than(broker, minutes=m, now=now, verbose=verbose)


def parse_per_symbol_buy_cooldown_min(entries_cfg: Mapping[str, Any] | None) -> float:
    """Resolve base buy cooldown minutes from ``entries``.

    Precedence: ``per_symbol_buy_cooldown_min`` → ``symbol_cooldown_minutes`` →
    ``min_minutes_since_last_entry_for_symbol`` (legacy).

    ``entries.symbol_cooldown_minutes`` is the scan-interval cooldown after last entry/add on a symbol
    (same gate as ``per_symbol_buy_cooldown_min``). It does **not** refer to ``execution.symbol_cooldown_minutes``
    (post-exit re-entry tier).
    """
    ec = dict(entries_cfg or {})
    raw = ec.get("per_symbol_buy_cooldown_min")
    if raw is None or str(raw).strip() == "":
        raw = ec.get("symbol_cooldown_minutes")
    if raw is None or str(raw).strip() == "":
        raw = ec.get("min_minutes_since_last_entry_for_symbol", 0)
    try:
        return float(raw) if raw is not None and str(raw).strip() != "" else 0.0
    except (TypeError, ValueError):
        return 0.0


def effective_per_symbol_buy_cooldown_min(
    entries_cfg: Mapping[str, Any] | None, symbol_upper: str
) -> float:
    """Base minutes from :func:`parse_per_symbol_buy_cooldown_min`, then ``leader_cooldown_overrides``.

    Populated from YAML ``entries.leader_cooldown_overrides`` or merged ``cooldowns.leader_overrides``
    (see :func:`~src.config_loader.load_config`).
    """
    base = parse_per_symbol_buy_cooldown_min(entries_cfg)
    ec = dict(entries_cfg or {})
    raw_ov = ec.get("leader_cooldown_overrides")
    if not isinstance(raw_ov, dict):
        return base
    key = str(symbol_upper).strip().upper()
    if not key:
        return base
    ov_val = raw_ov.get(key)
    if ov_val is None:
        for ok, val in raw_ov.items():
            if str(ok).strip().upper() == key:
                ov_val = val
                break
        else:
            return base
    try:
        if ov_val is None or str(ov_val).strip() == "":
            return base
        return float(ov_val)
    except (TypeError, ValueError):
        return base


def entry_scan_allowed_et(
    dt_et: datetime, entries_cfg: Mapping[str, Any] | None
) -> bool:
    """Respect ``enable_new_entries``, ``avoid_new_entries_before(_et)``, ``avoid_new_entries_after(_et)``.

    When ``avoid_new_entries_after`` is set (e.g. ``"15:00"`` for the last regular-session hour with a 16:00 close),
    new entries are disallowed at or after that wall-clock time (ET). Omitted or empty = no end-of-day window.
    """
    ec = dict(entries_cfg or {})
    if not bool(ec.get("enable_new_entries", ec.get("enabled", True))):
        return False
    raw_before = ec.get("avoid_new_entries_before_et") or ec.get("avoid_new_entries_before")
    if raw_before is not None and str(raw_before).strip() != "":
        parts = str(raw_before).strip().replace(":", " ").split()
        if len(parts) >= 2:
            try:
                h, m = int(parts[0]), int(parts[1])
                cutoff = dt_et.replace(hour=h, minute=m, second=0, microsecond=0)
                if dt_et < cutoff:
                    return False
            except ValueError:
                pass
    raw_after = ec.get("avoid_new_entries_after_et") or ec.get("avoid_new_entries_after")
    if raw_after is not None and str(raw_after).strip() != "":
        parts = str(raw_after).strip().replace(":", " ").split()
        if len(parts) >= 2:
            try:
                h, m = int(parts[0]), int(parts[1])
                end = dt_et.replace(hour=h, minute=m, second=0, microsecond=0)
                if dt_et >= end:
                    return False
            except ValueError:
                pass
    return True


def minutes_since_last_recorded_exit(
    engine: TradingEngine,
    symbol_upper: str,
    now_dt: datetime,
) -> float | None:
    """Wall-clock minutes since the latest recorded stop or profit exit for *symbol_upper*, or None if none."""
    return minutes_since_last_broker_structured_exit(
        engine.state, str(symbol_upper), now_dt
    )
