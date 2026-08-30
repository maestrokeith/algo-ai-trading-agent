"""Controlled live-equity runtime profile helpers."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from src.pilot_exit_management import broker_pilot_position_report, load_exit_status
from src.trading_control import trading_control_config

log = logging.getLogger(__name__)

RUNTIME_PROFILE_CONTROLLED_LIVE_EQUITY = "controlled_live_equity"
RUNTIME_PROFILE_BOUNDED_LIVE_PILOT = "bounded_live_pilot"
PREMARKET_PENDING_FIRST_EXIT_CYCLE = "PREMARKET_PENDING_FIRST_EXIT_CYCLE"


@dataclass(frozen=True)
class ControlledLiveLimits:
    """Resolved non-pilot live-equity bounds used by readiness and startup logs."""

    max_managed_positions: int
    per_order_max_notional: float
    per_order_max_pct: float
    per_symbol_max_pct: float
    strategy_allocation_cap_pct: float
    portfolio_exposure_cap_pct: float
    stock_capital_pct: float
    min_cash_reserve_pct: float
    daily_loss_limit_pct: float

    @property
    def valid(self) -> bool:
        return (
            self.max_managed_positions > 0
            and self.per_order_max_notional > 0.0
            and self.per_order_max_pct > 0.0
            and self.per_symbol_max_pct > 0.0
            and self.strategy_allocation_cap_pct > 0.0
            and self.portfolio_exposure_cap_pct > 0.0
            and self.daily_loss_limit_pct > 0.0
        )


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _float(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return float(default)
    return out if out == out else float(default)


def _pct(value: Any, default: float = 0.0) -> float:
    raw = _float(value, default)
    return raw * 100.0 if 0.0 < raw <= 1.0 else raw


def runtime_profile(config: Mapping[str, Any] | None) -> str:
    """Return the configured live-equity runtime profile."""

    tc = trading_control_config(config)
    explicit = str(tc.get("runtime_profile") or "").strip().lower()
    if explicit:
        return explicit
    controlled = _mapping(tc.get("controlled_live_equity"))
    if bool(controlled.get("enabled", False)):
        return RUNTIME_PROFILE_CONTROLLED_LIVE_EQUITY
    pilot = _mapping(tc.get("live_pilot"))
    if bool(pilot.get("enabled", False)):
        return RUNTIME_PROFILE_BOUNDED_LIVE_PILOT
    mode = str(tc.get("mode") or "").strip().lower()
    return mode or "unknown"


def controlled_live_equity_active(config: Mapping[str, Any] | None) -> bool:
    tc = trading_control_config(config)
    return str(tc.get("mode") or "").strip().lower() == "live" and runtime_profile(config) == RUNTIME_PROFILE_CONTROLLED_LIVE_EQUITY


def bounded_live_pilot_active(config: Mapping[str, Any] | None) -> bool:
    tc = trading_control_config(config)
    pilot = _mapping(tc.get("live_pilot"))
    return (
        str(tc.get("mode") or "").strip().lower() == "live"
        and bool(pilot.get("enabled", False))
        and runtime_profile(config) != RUNTIME_PROFILE_CONTROLLED_LIVE_EQUITY
    )


def controlled_live_limits(config: Mapping[str, Any] | None) -> ControlledLiveLimits:
    """Resolve concrete normal-live bounds from existing production config."""

    cfg = config or {}
    tc = trading_control_config(cfg)
    profile = _mapping(tc.get("controlled_live_equity"))
    portfolio = _mapping(cfg.get("portfolio"))
    allocator = _mapping(portfolio.get("capital_allocator"))
    risk = _mapping(cfg.get("risk"))
    risk_symbol = _mapping(risk.get("max_symbol_allocation_pct"))
    gates = _mapping(portfolio.get("exposure_gates"))
    pr = _mapping(cfg.get("portfolio_risk"))
    max_positions = int(_float(profile.get("max_managed_positions", allocator.get("max_positions", portfolio.get("max_positions", 0))), 0.0))
    per_order_pct = _pct(profile.get("max_single_order_notional_pct", allocator.get("max_single_order_notional_pct")), 0.0)
    per_order_notional = _float(profile.get("max_single_order_notional", allocator.get("max_single_order_notional")), 0.0)
    risk_default_symbol = risk_symbol.get("default", risk.get("max_symbol_allocation_pct"))
    symbol_pct = min(
        x for x in (
            _pct(profile.get("max_symbol_exposure_pct", portfolio.get("max_single_position_pct")), 0.0),
            _pct(risk_default_symbol, 0.0),
        )
        if x > 0.0
    ) if (_pct(profile.get("max_symbol_exposure_pct", portfolio.get("max_single_position_pct")), 0.0) > 0.0 or _pct(risk_default_symbol, 0.0) > 0.0) else 0.0
    strategy_pct = _pct(profile.get("strategy_allocation_cap_pct", portfolio.get("max_stock_capital_pct")), 0.0)
    portfolio_pct = min(
        x for x in (
            _pct(profile.get("portfolio_exposure_cap_pct", gates.get("max_total_exposure_frac")), 0.0),
            _pct(risk.get("max_total_exposure_pct"), 0.0),
        )
        if x > 0.0
    ) if (_pct(profile.get("portfolio_exposure_cap_pct", gates.get("max_total_exposure_frac")), 0.0) > 0.0 or _pct(risk.get("max_total_exposure_pct"), 0.0) > 0.0) else 0.0
    stock_pct = _pct(profile.get("stock_capital_pct", portfolio.get("max_stock_capital_pct")), 0.0)
    cash_reserve = _pct(profile.get("min_cash_reserve_pct", portfolio.get("min_cash_reserve_pct")), 0.0)
    daily_loss = _pct(profile.get("daily_loss_limit_pct", pr.get("max_daily_loss_pct")), 0.0)
    return ControlledLiveLimits(
        max_managed_positions=max_positions,
        per_order_max_notional=per_order_notional,
        per_order_max_pct=per_order_pct,
        per_symbol_max_pct=symbol_pct,
        strategy_allocation_cap_pct=strategy_pct,
        portfolio_exposure_cap_pct=portfolio_pct,
        stock_capital_pct=stock_pct,
        min_cash_reserve_pct=cash_reserve,
        daily_loss_limit_pct=daily_loss,
    )


def controlled_live_limit_blockers(config: Mapping[str, Any] | None) -> list[str]:
    limits = controlled_live_limits(config)
    blockers: list[str] = []
    if limits.max_managed_positions <= 0:
        blockers.append("controlled_live_max_managed_positions_invalid")
    if limits.per_order_max_notional <= 0.0 or limits.per_order_max_pct <= 0.0:
        blockers.append("controlled_live_per_order_cap_invalid")
    if limits.per_symbol_max_pct <= 0.0:
        blockers.append("controlled_live_symbol_cap_invalid")
    if limits.strategy_allocation_cap_pct <= 0.0:
        blockers.append("controlled_live_strategy_cap_invalid")
    if limits.portfolio_exposure_cap_pct <= 0.0:
        blockers.append("controlled_live_portfolio_cap_invalid")
    if limits.daily_loss_limit_pct <= 0.0:
        blockers.append("controlled_live_daily_loss_cap_invalid")
    return blockers


def et_now() -> datetime:
    return datetime.now(ZoneInfo("America/New_York"))


def regular_session_open(now: datetime | None = None) -> bool:
    dt = now or et_now()
    if dt.weekday() >= 5:
        return False
    minutes = dt.hour * 60 + dt.minute
    return (9 * 60 + 30) <= minutes <= (16 * 60)


def premarket_pending_first_exit_cycle_allowed(
    *,
    requested_day: str,
    managed_symbols: Sequence[str],
    unknown_positions: int | str,
    open_broker_orders: int | str,
    now: datetime | None = None,
) -> bool:
    dt = now or et_now()
    if str(requested_day) != dt.date().isoformat():
        return False
    if regular_session_open(dt):
        return False
    if not managed_symbols:
        return False
    return unknown_positions == 0 and open_broker_orders == 0


def controlled_live_exit_health(
    *,
    config: Mapping[str, Any],
    broker: Any,
    data_dir: Path | str,
    user_id: str,
    day: str,
    now: datetime | None = None,
) -> tuple[bool, str]:
    """Validate that controlled-live entries are not allowed while exit management is stale."""

    try:
        positions = broker.get_positions()
        report = broker_pilot_position_report(
            config=config,
            positions=positions,
            data_dir=data_dir,
            user_id=user_id,
            day=day,
        )
        managed = sorted(
            str(row.get("symbol") or "").strip().upper()
            for row in report.get("position_classifications") or []
            if isinstance(row, Mapping) and str(row.get("classification") or "").strip().upper() == "PILOT_MANAGED"
        )
        if not managed:
            return True, "no_managed_positions"
        status = load_exit_status(data_dir, user_id, day)
        rows = status.get("positions") if isinstance(status.get("positions"), Mapping) else {}
        missing = [sym for sym in managed if not (isinstance(rows.get(sym), Mapping) and rows[sym].get("last_exit_eval_at"))]
        if not missing:
            return True, "healthy"
        if not regular_session_open(now):
            return True, PREMARKET_PENDING_FIRST_EXIT_CYCLE
        return False, "exit_manager_unhealthy:" + ",".join(missing)
    except Exception as exc:
        log.exception("CONTROLLED_LIVE_EXIT_HEALTH_ERROR")
        return False, f"exit_manager_unknown:{type(exc).__name__}"


def emit_controlled_live_equity_startup(config: Mapping[str, Any] | None, *, managed_positions: int = 0) -> None:
    if not controlled_live_equity_active(config):
        return
    limits = controlled_live_limits(config)
    tc = trading_control_config(config)
    states = _mapping(tc.get("strategy_states"))
    live = ",".join(sorted(k for k, v in states.items() if str(v).strip().upper() == "LIVE"))
    pilot = _mapping(tc.get("live_pilot"))
    protected = ",".join(str(s).strip().upper() for s in pilot.get("preexisting_position_allowlist", []) if str(s).strip())
    msg = (
        "CONTROLLED_LIVE_EQUITY_CONFIG mode=live runtime_profile=controlled_live_equity "
        "live_strategies=%s options_status=inactive max_managed_positions=%d "
        "per_order_cap=min(%.2f%%_equity,%.2f) per_symbol_cap=%.2f%% "
        "strategy_allocation_cap=%.2f%% portfolio_deployment_cap=%.2f%% daily_loss_cap=%.2f%% "
        "preexisting_protected_symbols=%s current_managed_positions=%d fail_closed=true"
        % (
            live or "none",
            limits.max_managed_positions,
            limits.per_order_max_pct,
            limits.per_order_max_notional,
            limits.per_symbol_max_pct,
            limits.strategy_allocation_cap_pct,
            limits.portfolio_exposure_cap_pct,
            limits.daily_loss_limit_pct,
            protected or "none",
            int(managed_positions),
        )
    )
    log.warning(msg)
    print(msg, flush=True)
