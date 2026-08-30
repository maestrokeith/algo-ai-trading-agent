"""Trading mode, strategy state, and entry-safety controls."""
from __future__ import annotations

import json
import logging
import os
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

log = logging.getLogger(__name__)

VALID_TRADING_MODES = {"live", "paper", "shadow", "entries-disabled"}
VALID_STRATEGY_STATES = {"LIVE", "SHADOW", "DISABLED"}

ENTRY_BLOCKED_MODE_ENTRIES_DISABLED = "ENTRY_BLOCKED_MODE_ENTRIES_DISABLED"
ENTRY_BLOCKED_INTEGRITY_FAILURE = "ENTRY_BLOCKED_INTEGRITY_FAILURE"
ENTRY_BLOCKED_SYSTEM_ERROR = "ENTRY_BLOCKED_SYSTEM_ERROR"
EXPECTED_ENTRY_BLOCK_REASONS = {
    ENTRY_BLOCKED_MODE_ENTRIES_DISABLED,
    "ENTRY_BLOCKED_SHADOW_MODE",
    "ORDER_BLOCKED_SHADOW_MODE",
    "preexisting_position_symbol",
    "strategy_not_live",
    "strategy_not_allowed",
    "risk_limit",
    "exposure_limit",
    "existing_position",
    "open_position_limit",
    "daily_submission_limit",
    "daily_trade_limit",
    "daily_fill_limit",
    "order_notional_above_cap",
    "deployed_notional_above_cap",
    "same_symbol_reentry_blocked",
}


@dataclass(frozen=True)
class TradingModeState:
    """Resolved runtime trading mode."""

    mode: str
    live_orders_allowed: bool
    new_entries_allowed: bool
    broker_orders_allowed: bool


@dataclass(frozen=True)
class StrategyState:
    """Configured production state for a strategy route."""

    route: str
    state: str


class TradingModeError(ValueError):
    """Raised when trading mode configuration is unsafe."""


class EntryBlocked(RuntimeError):
    """Raised to deny unsafe order submission without crashing safe exits."""


def is_expected_entry_block(reason_code: Any) -> bool:
    """Return whether an entry block is normal control flow, not an incident."""

    return str(reason_code or "").strip() in EXPECTED_ENTRY_BLOCK_REASONS


def trading_control_config(config: Mapping[str, Any] | None) -> Mapping[str, Any]:
    raw = (config or {}).get("trading_control")
    return raw if isinstance(raw, Mapping) else {}


def resolve_trading_mode(
    config: Mapping[str, Any] | None,
    *,
    cli_mode: str | None = None,
    paper: bool | None = None,
    live_operation: bool = False,
) -> TradingModeState:
    """Resolve operating mode from CLI override, config, or paper/live context.

    Live operation fails closed unless the resolved mode is explicit and valid.
    """

    cfg = trading_control_config(config)
    raw = cli_mode or cfg.get("mode")
    if raw is None or str(raw).strip() == "":
        if live_operation:
            raise TradingModeError("trading_control.mode is required for live operation")
        raw = "paper" if paper else "live"
    mode = str(raw).strip().lower()
    if mode not in VALID_TRADING_MODES:
        raise TradingModeError(f"unknown trading mode: {mode}")
    if mode == "paper":
        return TradingModeState(mode, False, True, True)
    if mode == "shadow":
        return TradingModeState(mode, False, True, False)
    if mode == "entries-disabled":
        return TradingModeState(mode, True, False, True)
    return TradingModeState(mode, True, True, True)


def emit_trading_mode_startup(state: TradingModeState) -> None:
    line = (
        "TRADING_MODE mode=%s live_orders_allowed=%s new_entries_allowed=%s"
        % (
            state.mode,
            str(bool(state.live_orders_allowed)).lower(),
            str(bool(state.new_entries_allowed)).lower(),
        )
    )
    log.warning(line)
    print(line, flush=True)


def strategy_states(config: Mapping[str, Any] | None) -> dict[str, StrategyState]:
    cfg = trading_control_config(config)
    raw = cfg.get("strategy_states")
    if not isinstance(raw, Mapping):
        raw = {}
    routes = set(str(k) for k in raw)
    routes.update({"trend_long", "dynamic_no_catalyst", "news_only", "momentum_breakout", "options_live", "options_paper"})
    out: dict[str, StrategyState] = {}
    for route in sorted(routes):
        value = raw.get(route, "SHADOW")
        state = str(value or "SHADOW").strip().upper()
        if state not in VALID_STRATEGY_STATES:
            state = "SHADOW"
        out[route] = StrategyState(route=route, state=state)
    return out


def emit_strategy_state_startup(config: Mapping[str, Any] | None) -> None:
    for row in strategy_states(config).values():
        line = f"STRATEGY_STATE route={row.route} state={row.state}"
        log.info(line)
        print(line, flush=True)


def strategy_allows_live_entry(config: Mapping[str, Any] | None, route: str) -> bool:
    state = strategy_states(config).get(str(route or "").strip() or "unknown")
    return bool(state and state.state == "LIVE")


def order_is_new_entry(order: Any) -> bool:
    side = str(getattr(order, "side", "") or "").strip().lower()
    return side == "buy"


def authorize_order_submission(
    config: Mapping[str, Any] | None,
    order: Any,
    *,
    paper: bool | None = None,
    data_dir: Path | str | None = None,
    user_id: str | None = None,
    reserve_live_pilot: bool = False,
) -> tuple[bool, str | None]:
    """Return whether a broker order may be submitted under the current mode."""

    state = resolve_trading_mode(
        config,
        paper=paper,
        live_operation=not bool(paper),
    )
    if state.mode == "shadow":
        return False, "ENTRY_BLOCKED_SHADOW_MODE" if order_is_new_entry(order) else "ORDER_BLOCKED_SHADOW_MODE"
    if order_is_new_entry(order) and not state.new_entries_allowed:
        return False, ENTRY_BLOCKED_MODE_ENTRIES_DISABLED
    if not state.broker_orders_allowed:
        return False, "ORDER_BLOCKED_TRADING_MODE"
    if state.mode == "live":
        try:
            from src.limited_live_pilot import is_preexisting_position_symbol

            if is_preexisting_position_symbol(config, getattr(order, "symbol", None)):
                return False, "preexisting_position_symbol"
        except Exception:
            log.exception("PREEXISTING_POSITION_GUARD_ERROR")
            return False, "preexisting_position_guard_error"
    try:
        from src.controlled_live_equity import bounded_live_pilot_active
        from src.limited_live_pilot import validate_pilot_order

        is_bounded_profile = bounded_live_pilot_active(config)
        if state.mode == "live" and order_is_new_entry(order) and not is_bounded_profile:
            route = getattr(order, "route", None) or getattr(order, "strategy", None) or getattr(order, "source", None)
            if not route or not strategy_allows_live_entry(config, str(route)):
                return False, "strategy_not_allowed"
        if is_bounded_profile:
            if order_is_new_entry(order) and bool(getattr(order, "_limited_live_reserved", False)):
                return True, None
            if data_dir is None or not user_id:
                return False, "limited_live_context_missing"
            decision = validate_pilot_order(config, order, data_dir=data_dir, user_id=str(user_id))
            if not decision.allowed:
                return False, decision.reason or "limited_live_blocked"
    except Exception:
        log.exception("LIMITED_LIVE_GUARD_ERROR")
        return False, "limited_live_guard_error"
    return True, None


def order_submission_block_event(
    config: Mapping[str, Any] | None,
    order: Any,
    *,
    reason: str | None,
    paper: bool | None = None,
    user_id: str | None = None,
    route: str | None = None,
) -> dict[str, Any]:
    """Return structured diagnostics for a broker-boundary submission block."""

    try:
        state = resolve_trading_mode(config, paper=paper, live_operation=not bool(paper))
        effective_mode = state.mode
        broker_orders_allowed = state.broker_orders_allowed
        new_entries_allowed = state.new_entries_allowed
    except Exception:
        effective_mode = "invalid"
        broker_orders_allowed = False
        new_entries_allowed = False
    configured_mode = trading_control_config(config).get("mode")
    return {
        "event": "BROKER_DISPATCH_BLOCKED",
        "reason": reason or "ORDER_BLOCKED_TRADING_MODE",
        "configured_mode": str(configured_mode or "missing").strip().lower() or "missing",
        "effective_mode": effective_mode,
        "broker_orders_allowed": bool(broker_orders_allowed),
        "new_entries_allowed": bool(new_entries_allowed),
        "paper": bool(paper) if paper is not None else None,
        "user_id": user_id,
        "symbol": getattr(order, "symbol", None),
        "route": route or getattr(order, "route", None) or getattr(order, "source", None),
        "action": getattr(order, "side", None),
        "broker_dispatch_attempted": False,
        "execution_allowed": False,
        "hypothetical": effective_mode == "shadow",
    }


def log_order_submission_block(
    config: Mapping[str, Any] | None,
    order: Any,
    *,
    reason: str | None,
    paper: bool | None = None,
    user_id: str | None = None,
    route: str | None = None,
) -> dict[str, Any]:
    event = order_submission_block_event(config, order, reason=reason, paper=paper, user_id=user_id, route=route)
    log_method = log.info if is_expected_entry_block(event["reason"]) else log.error
    if event["reason"] == "preexisting_position_symbol":
        log.error(
            "PREEXISTING_POSITION_ACTION_BLOCKED reason=%s configured_mode=%s effective_mode=%s user_id=%s symbol=%s route=%s action=%s broker_dispatch_attempted=false execution_allowed=false",
            event["reason"],
            event["configured_mode"],
            event["effective_mode"],
            event.get("user_id") or "n/a",
            event.get("symbol") or "n/a",
            event.get("route") or "n/a",
            event.get("action") or "n/a",
        )
    log_method(
        "BROKER_DISPATCH_BLOCKED reason=%s configured_mode=%s effective_mode=%s user_id=%s symbol=%s route=%s action=%s broker_dispatch_attempted=false execution_allowed=false hypothetical=%s",
        event["reason"],
        event["configured_mode"],
        event["effective_mode"],
        event.get("user_id") or "n/a",
        event.get("symbol") or "n/a",
        event.get("route") or "n/a",
        event.get("action") or "n/a",
        str(bool(event.get("hypothetical"))).lower(),
    )
    return event


def integrity_state_path(data_dir: Path | str, user_id: str) -> Path:
    return Path(data_dir) / "integrity" / f"{user_id}.json"


def load_integrity_state(data_dir: Path | str, user_id: str) -> dict[str, Any]:
    path = integrity_state_path(data_dir, user_id)
    if not path.exists():
        return {"entry_blocked": False, "incidents": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"entry_blocked": True, "incidents": [{"reason_code": ENTRY_BLOCKED_INTEGRITY_FAILURE, "detail": "integrity state unreadable"}]}
    return data if isinstance(data, dict) else {"entry_blocked": True, "incidents": []}


def persist_integrity_incident(
    data_dir: Path | str,
    *,
    user_id: str,
    reason_code: str,
    severity: str = "high",
    detail: str = "",
    context: Mapping[str, Any] | None = None,
) -> Path:
    classification = "EXPECTED_SAFETY_BLOCK" if is_expected_entry_block(reason_code) else "INTEGRITY_INCIDENT"
    if classification == "EXPECTED_SAFETY_BLOCK" and severity == "high":
        severity = "warning"
    path = integrity_state_path(data_dir, user_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    state = load_integrity_state(data_dir, user_id)
    incidents = state.get("incidents") if isinstance(state.get("incidents"), list) else []
    incidents.append(
        {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "severity": severity,
            "reason_code": reason_code,
            "classification": classification,
            "detail": detail,
            "context": dict(context or {}),
        }
    )
    state["entry_blocked"] = bool(classification != "EXPECTED_SAFETY_BLOCK")
    state["last_reason_code"] = reason_code
    state["incidents"] = incidents
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)
    log.error(
        "INTEGRITY_INCIDENT severity=%s user_id=%s reason_code=%s detail=%s",
        severity,
        user_id,
        reason_code,
        detail,
    )
    return path


def entries_blocked_by_integrity(data_dir: Path | str, user_id: str) -> bool:
    return bool(load_integrity_state(data_dir, user_id).get("entry_blocked"))


class EntryCircuitBreaker:
    """Global new-entry circuit breaker for repeated runtime/integrity failures."""

    def __init__(self, *, threshold: int = 3) -> None:
        self.threshold = max(1, int(threshold))
        self.failures = 0

    def record_failure(self) -> bool:
        self.failures += 1
        return self.open

    def reset(self) -> None:
        self.failures = 0

    @property
    def open(self) -> bool:
        return self.failures >= self.threshold


def run_entry_evaluation_safely(
    func: Callable[..., Any],
    *,
    data_dir: Path | str,
    user_id: str,
    context: Mapping[str, Any] | None = None,
    circuit_breaker: EntryCircuitBreaker | None = None,
    **kwargs: Any,
) -> Any | None:
    """Execute one entry evaluation and fail closed for new entries on exceptions."""

    try:
        result = func(**kwargs)
        if circuit_breaker is not None:
            circuit_breaker.reset()
        return result
    except Exception as exc:
        tb = traceback.format_exc()
        ctx = dict(context or {})
        ctx.update({"exception_type": type(exc).__name__, "traceback": tb})
        persist_integrity_incident(
            data_dir,
            user_id=user_id,
            reason_code=ENTRY_BLOCKED_SYSTEM_ERROR,
            detail=str(exc),
            context=ctx,
        )
        if circuit_breaker is not None and circuit_breaker.record_failure():
            persist_integrity_incident(
                data_dir,
                user_id=user_id,
                reason_code=ENTRY_BLOCKED_INTEGRITY_FAILURE,
                detail="global entry circuit breaker open",
                context={"failures": circuit_breaker.failures, "threshold": circuit_breaker.threshold},
            )
        log.exception("ENTRY_BLOCKED_SYSTEM_ERROR user_id=%s context=%s", user_id, ctx)
        return None


class ShadowOrder:
    """Broker-order stand-in for shadow mode."""

    def __init__(self, order: Any) -> None:
        self.id = f"shadow-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}"
        self.status = "shadow"
        self.symbol = getattr(order, "symbol", None)
        self.side = getattr(order, "side", None)
        self.qty = getattr(order, "quantity", None)
        self.filled_qty = 0
        self.filled_avg_price = None


class TradingControlBroker:
    """Broker wrapper that enforces trading mode before order submission."""

    def __init__(self, broker: Any, *, config: Mapping[str, Any], paper: bool, data_dir: Path | str, user_id: str) -> None:
        self._broker = broker
        self._config = config
        self._paper = paper
        self._data_dir = Path(data_dir)
        self._user_id = user_id

    def __getattr__(self, name: str) -> Any:
        return getattr(self._broker, name)

    def _attach_metadata(self, order: Any) -> None:
        route = getattr(order, "route", None) or getattr(order, "strategy", None) or getattr(order, "strategy_route", None) or getattr(order, "source", None)
        if route and not getattr(order, "route", None):
            setattr(order, "route", str(route).strip())
        source = getattr(order, "source", None) or route
        if source and not getattr(order, "source", None):
            setattr(order, "source", str(source).strip())
        strategy = getattr(order, "strategy", None) or route
        if strategy and not getattr(order, "strategy", None):
            setattr(order, "strategy", str(strategy).strip())
        setattr(order, "user_id", self._user_id)
        setattr(order, "instrument_type", "option" if str(getattr(order, "symbol", "") or "").upper()[-8:].isdigit() else "equity")
        try:
            from src.controlled_live_equity import bounded_live_pilot_active

            setattr(order, "limited_live_pilot", bool(bounded_live_pilot_active(self._config)))
        except Exception:
            setattr(order, "limited_live_pilot", False)

    def submit_order(self, order: Any) -> Any:
        self._attach_metadata(order)
        try:
            from src.controlled_live_equity import bounded_live_pilot_active, controlled_live_equity_active, controlled_live_exit_health
            from src.limited_live_pilot import adjust_pilot_order_size, finalize_pilot_submission_reservation, reserve_pilot_submission, trading_day_et

            if bounded_live_pilot_active(self._config):
                sizing = adjust_pilot_order_size(
                    self._config,
                    order,
                    data_dir=self._data_dir,
                    user_id=self._user_id,
                    broker=self._broker,
                )
                if not sizing.allowed:
                    allowed, reason = False, sizing.reason or "limited_live_size_blocked"
                else:
                    allowed, reason = authorize_order_submission(
                        self._config,
                        order,
                        paper=self._paper,
                        data_dir=self._data_dir,
                        user_id=self._user_id,
                        reserve_live_pilot=True,
                    )
            else:
                allowed, reason = authorize_order_submission(
                    self._config,
                    order,
                    paper=self._paper,
                    data_dir=self._data_dir,
                    user_id=self._user_id,
                    reserve_live_pilot=True,
                )
                if allowed and controlled_live_equity_active(self._config) and order_is_new_entry(order):
                    healthy, exit_reason = controlled_live_exit_health(
                        config=self._config,
                        broker=self._broker,
                        data_dir=self._data_dir,
                        user_id=self._user_id,
                        day=trading_day_et(),
                    )
                    if not healthy:
                        allowed, reason = False, exit_reason
        except Exception:
            log.exception("LIMITED_LIVE_SIZE_GUARD_ERROR")
            allowed, reason = False, "limited_live_size_guard_error"
        if allowed:
            reserved = False
            mode_state = resolve_trading_mode(self._config, paper=self._paper, live_operation=not bool(self._paper))
            pilot_active = mode_state.mode == "live" and bool(bounded_live_pilot_active(self._config))
            if pilot_active and order_is_new_entry(order):
                reservation = reserve_pilot_submission(
                    self._config,
                    order,
                    data_dir=self._data_dir,
                    user_id=self._user_id,
                )
                if not reservation.allowed:
                    allowed, reason = False, reservation.reason or "limited_live_reservation_blocked"
                else:
                    reserved = bool(reservation.reserved)
            if allowed:
                try:
                    log.info(
                        "LIMITED_LIVE_BROKER_DISPATCH_ATTEMPT user_id=%s symbol=%s route=%s source=%s strategy=%s reservation_id=%s broker_dispatch_attempted=true execution_allowed=true",
                        self._user_id,
                        getattr(order, "symbol", "n/a"),
                        getattr(order, "route", None) or "n/a",
                        getattr(order, "source", None) or "n/a",
                        getattr(order, "strategy", None) or "n/a",
                        getattr(order, "_limited_live_reservation_id", None) or "n/a",
                    )
                    result = self._broker.submit_order(order)
                    log.info(
                        "LIMITED_LIVE_BROKER_SUBMISSION_SUCCEEDED user_id=%s symbol=%s route=%s source=%s strategy=%s reservation_id=%s broker_dispatch_attempted=true execution_allowed=true",
                        self._user_id,
                        getattr(order, "symbol", "n/a"),
                        getattr(order, "route", None) or "n/a",
                        getattr(order, "source", None) or "n/a",
                        getattr(order, "strategy", None) or "n/a",
                        getattr(order, "_limited_live_reservation_id", None) or "n/a",
                    )
                    return result
                except Exception:
                    log.exception(
                        "LIMITED_LIVE_BROKER_SUBMISSION_FAILED user_id=%s symbol=%s route=%s source=%s strategy=%s reservation_id=%s broker_dispatch_attempted=true execution_allowed=true",
                        self._user_id,
                        getattr(order, "symbol", "n/a"),
                        getattr(order, "route", None) or "n/a",
                        getattr(order, "source", None) or "n/a",
                        getattr(order, "strategy", None) or "n/a",
                        getattr(order, "_limited_live_reservation_id", None) or "n/a",
                    )
                    raise
                finally:
                    if reserved:
                        finalize_pilot_submission_reservation(
                            self._data_dir,
                            self._user_id,
                            order=order,
                            reason="broker_dispatch_completed",
                        )
        reason_code = reason or "ORDER_BLOCKED_TRADING_MODE"
        try:
            if str(reason_code).startswith("options_blocked"):
                log.error(
                    "OPTIONS_BLOCKED_LIVE_PILOT reason=%s user_id=%s symbol=%s broker_dispatch_attempted=false",
                    reason_code,
                    self._user_id,
                    getattr(order, "symbol", "n/a"),
                )
            elif reason_code == "preexisting_position_symbol":
                log.error(
                    "PREEXISTING_POSITION_ACTION_BLOCKED reason=%s user_id=%s symbol=%s broker_dispatch_attempted=false",
                    reason_code,
                    self._user_id,
                    getattr(order, "symbol", "n/a"),
                )
            elif str(reason_code).startswith("limited_live") or reason_code in {
                "daily_submission_limit",
                "daily_trade_limit",
                "daily_fill_limit",
                "open_position_limit",
                "same_symbol_reentry_blocked",
                "order_notional_above_cap",
                "deployed_notional_above_cap",
                "invalid_reference_price",
                "final_pilot_notional_invalid",
                "minimum_share_cost_exceeds_cap",
                "daily_loss_lock",
                "strategy_not_allowed",
                "short_selling_blocked",
                "replacement_blocked",
                "add_to_existing_blocked",
                "reallocation_blocked",
                "pilot_submission_lock_busy",
                "missing_order_notional",
                "preexisting_position_symbol",
            }:
                log.error(
                    "LIMITED_LIVE_ORDER_BLOCKED reason=%s user_id=%s symbol=%s broker_dispatch_attempted=false",
                    reason_code,
                    self._user_id,
                    getattr(order, "symbol", "n/a"),
                )
        except Exception:
            pass
        block_event = log_order_submission_block(
            self._config,
            order,
            reason=reason_code,
            paper=self._paper,
            user_id=self._user_id,
        )
        if not is_expected_entry_block(reason_code):
            persist_integrity_incident(
                self._data_dir,
                user_id=self._user_id,
                reason_code=reason_code,
                detail="broker submission blocked by trading mode",
                context=block_event,
            )
        if reason_code in {"ENTRY_BLOCKED_SHADOW_MODE", "ORDER_BLOCKED_SHADOW_MODE"}:
            return ShadowOrder(order)
        raise EntryBlocked(reason_code)
