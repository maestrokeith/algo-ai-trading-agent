"""Read-only options readiness checks shared by startup, AutoOps, and CLI."""
from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from src.config_loader import deep_merge, load_config
from src.live.options_chain import broker_mode_is_paper
from src.options_config import (
    allow_new_entries,
    max_bid_ask_spread_pct_cap,
    max_open_option_positions_cap,
    max_premium_per_trade_usd,
    options_enabled,
    options_live_pilot_enabled,
    options_mode,
    target_dte_bounds,
)
from src.options_daily_limit import build_options_daily_limit_usage


LIVE_OPTION_MODES = {"live", "live_long_premium", "long_premium_only"}


@dataclass(frozen=True)
class OptionsReadiness:
    user_id: str
    environment: str
    config_enabled: bool
    mode: str
    live_pilot_enabled: bool
    long_premium_only: bool
    broker_mode: str
    broker_supported: bool
    runtime_entry_enabled: bool
    market_data_ready: bool
    option_chain_access: bool
    broker_options_permission: bool
    buying_power_ready: bool
    exit_automation_ready: bool
    options_trade_executed_today: bool
    scanner_ready: bool
    entry_router_ready: bool
    risk_ready: bool
    route_active: bool
    risk_limits_safe: bool
    positions_open: int
    max_positions: int
    trades_today: int
    max_trades_per_day: int
    max_contracts_per_trade: int
    daily_limit_excluded: int
    daily_limit_date: str
    final_status: str
    blocking_reasons: tuple[str, ...]


def _as_bool(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return bool(default)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "on", "y"}:
            return True
        if text in {"0", "false", "no", "off", "n", ""}:
            return False
    return bool(value)


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if out == out else default


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _options(config: Mapping[str, Any] | None) -> Mapping[str, Any]:
    opts = (config or {}).get("options") if isinstance(config, Mapping) else {}
    return opts if isinstance(opts, Mapping) else {}


def load_effective_runtime_config(
    root: str | Path,
    *,
    environment: str,
    user_id: str | None = None,
) -> dict[str, Any]:
    """Load default config merged with the selected user override without credentials."""
    repo_root = Path(root)
    default_path = repo_root / "config" / "default.yaml"
    if not default_path.exists():
        repo_root = Path(__file__).resolve().parents[1]
        default_path = repo_root / "config" / "default.yaml"
    cfg = load_config(default_path)
    requested_users_path = Path(root) / "config" / "users.yaml"
    users_path = requested_users_path if requested_users_path.exists() else repo_root / "config" / "users.yaml"
    selected = str(user_id or ("live_bot" if str(environment).lower() == "live" else "paper_bot"))
    if not users_path.exists():
        return cfg
    import yaml

    payload = yaml.safe_load(users_path.read_text(encoding="utf-8")) or {}
    entries = payload.get("users") if isinstance(payload, Mapping) else []
    for entry in entries or []:
        if not isinstance(entry, Mapping):
            continue
        if str(entry.get("id") or "") != selected:
            continue
        overrides = entry.get("overrides") if isinstance(entry.get("overrides"), Mapping) else {}
        return deep_merge(cfg, dict(overrides))
    return cfg


def broker_options_supported(broker: Any | None = None) -> bool:
    """Return true when the broker surface exposes the required option data/order hooks."""
    if broker is not None:
        return callable(getattr(broker, "get_option_chain_candidates", None))
    try:
        from src.brokers.alpaca_client import AlpacaBroker
    except Exception:
        return False
    return callable(getattr(AlpacaBroker, "get_option_chain_candidates", None))


def _broker_options_permission(broker: Any | None = None) -> bool:
    if broker is None:
        return broker_options_supported(None)
    raw = getattr(broker, "options_trading_enabled", None)
    if raw is not None:
        return _as_bool(raw)
    raw = getattr(broker, "broker_options_supported", None)
    if raw is not None:
        return _as_bool(raw)
    fn = getattr(broker, "options_supported", None)
    if callable(fn):
        try:
            return _as_bool(fn())
        except Exception:
            return False
    return broker_options_supported(broker)


def _buying_power_ready(broker: Any | None = None) -> bool:
    if broker is None:
        return True
    for attr in ("options_buying_power", "buying_power", "cash"):
        raw = getattr(broker, attr, None)
        if raw is None:
            continue
        if callable(raw):
            try:
                raw = raw()
            except Exception:
                return False
        return _as_float(raw, 0.0) > 0.0
    getter = getattr(broker, "get_account_snapshot", None)
    if callable(getter):
        try:
            snap = getter()
        except Exception:
            return False
        if isinstance(snap, Mapping):
            for key in ("options_buying_power", "buying_power", "cash"):
                if key in snap:
                    return _as_float(snap.get(key), 0.0) > 0.0
    return True


def _long_premium_only(config: Mapping[str, Any]) -> bool:
    opts = _options(config)
    if not _as_bool(opts.get("only_buy_options"), default=True):
        return False
    allowed_types = opts.get("allowed_contract_types")
    if allowed_types is not None:
        allowed = {str(item or "").strip().lower() for item in allowed_types}
        if not allowed or not allowed.issubset({"call", "put"}):
            return False
    mapping = opts.get("entry_mapping") if isinstance(opts.get("entry_mapping"), Mapping) else {}
    bullish = str(mapping.get("bullish_signal") or "call").strip().lower()
    bearish = str(mapping.get("bearish_signal") or "put").strip().lower()
    return bullish in {"call", "calls"} and bearish in {"put", "puts"}


def _contract_selection_safe(config: Mapping[str, Any]) -> bool:
    opts = _options(config)
    cs = opts.get("contract_selection") if isinstance(opts.get("contract_selection"), Mapping) else {}
    min_oi = _as_int(cs.get("min_open_interest"), 0)
    min_vol = _as_int(cs.get("min_volume"), 0)
    dte_min, dte_max = target_dte_bounds(dict(config))
    spread = max_bid_ask_spread_pct_cap(dict(config))
    return min_oi > 0 and min_vol > 0 and dte_min >= 1 and dte_max >= dte_min and 0 < spread <= 12.0


def _risk_limits_safe(config: Mapping[str, Any]) -> bool:
    opts = _options(config)
    total_exposure = _as_float(opts.get("total_exposure_limit", opts.get("max_total_options_exposure_pct")), 0.0)
    per_trade = _as_float(opts.get("per_trade", opts.get("max_option_position_pct")), 0.0)
    daily_loss_dollars = _as_float(opts.get("max_daily_options_loss_dollars", opts.get("max_daily_loss_dollars")), 0.0)
    daily_loss_pct = _as_float(opts.get("max_daily_options_loss_percent", opts.get("max_daily_loss_pct")), 0.0)
    max_premium = max_premium_per_trade_usd(dict(config))
    return (
        max_open_option_positions_cap(dict(config)) == 1
        and _as_int(opts.get("max_option_trades_per_day"), 0) == 1
        and _as_int(opts.get("max_contracts_per_trade", opts.get("v1_max_contracts_per_trade")), 0) == 1
        and 0 < total_exposure <= 0.02
        and 0 < per_trade <= 0.02
        and max_premium is not None
        and max_premium > 0
        and (daily_loss_dollars > 0 or daily_loss_pct > 0)
        and bool(opts.get("allowed_underlyings"))
        and _contract_selection_safe(config)
        and _long_premium_only(config)
        and _as_bool(opts.get("never_bypass_stock_risk_caps"), default=True)
        and not _as_bool((opts.get("bypass_when_full") or {}).get("allow_when_full") if isinstance(opts.get("bypass_when_full"), Mapping) else False)
    )


def _load_positions_count(root: Path, user_id: str) -> int:
    path = root / "data" / f"options_positions_{user_id}.json"
    if not path.exists():
        return 0
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    positions = payload.get("positions") if isinstance(payload, Mapping) else {}
    if isinstance(positions, Mapping):
        return sum(1 for row in positions.values() if isinstance(row, Mapping) and _as_float(row.get("qty"), 0.0) > 0)
    return 0


def build_options_readiness(
    config: Mapping[str, Any],
    *,
    environment: str,
    user_id: str = "live_bot",
    root: str | Path | None = None,
    broker: Any | None = None,
) -> OptionsReadiness:
    """Evaluate whether options can route, without fetching chains or placing orders."""
    env = str(environment or "paper").strip().lower()
    root_path = Path(root or Path(__file__).resolve().parents[1])
    opts = _options(config)
    enabled = options_enabled(dict(config))
    mode = options_mode(dict(config))
    live_pilot = options_live_pilot_enabled(dict(config))
    broker_paper = broker_mode_is_paper(broker, dict(config)) if broker is not None else env != "live"
    broker_mode = "paper" if broker_paper else "live"
    broker_supported = broker_options_supported(broker)
    option_chain_access = broker_supported
    broker_permission = _broker_options_permission(broker)
    long_only = _long_premium_only(config)
    risk_safe = _risk_limits_safe(config)
    positions = _load_positions_count(root_path, user_id)
    max_positions = max_open_option_positions_cap(dict(config))
    max_trades = _as_int(opts.get("max_option_trades_per_day"), 0)
    daily_usage = build_options_daily_limit_usage(
        root=root_path,
        user_id=user_id,
        environment=env,
        limit=max_trades,
    )
    trades = daily_usage.counted
    max_contracts = _as_int(opts.get("max_contracts_per_trade", opts.get("v1_max_contracts_per_trade")), 1)
    runtime_entry_enabled = allow_new_entries(dict(config))
    market_data_ready = broker_supported and bool(opts.get("allowed_underlyings"))
    buying_power_ready = _buying_power_ready(broker)
    exits = opts.get("exits") if isinstance(opts.get("exits"), Mapping) else {}
    exit_auto = _as_bool(exits.get("automation_enabled"), default=True)
    trade_executed_today = trades > 0
    scanner_ready = bool(opts.get("allowed_underlyings"))
    entry_router_ready = long_only and bool(opts.get("entry_mapping"))
    reasons: list[str] = []
    if not enabled:
        reasons.append("options_disabled")
    if env == "live":
        if mode not in LIVE_OPTION_MODES:
            reasons.append("mode_not_live")
        if not live_pilot:
            reasons.append("live_pilot_disabled")
    elif mode not in {"paper_only", "long_premium_only", "scan_only", "shadow_live", "live_long_premium", "live"}:
        reasons.append("mode_not_paper_capable")
    if not long_only:
        reasons.append("not_long_premium_only")
    if not broker_supported:
        reasons.append("broker_not_supported")
    if not broker_permission:
        reasons.append("broker_options_not_supported")
    if not option_chain_access:
        reasons.append("option_chain_access_unavailable")
    if not market_data_ready:
        reasons.append("market_data_unavailable")
    if not buying_power_ready:
        reasons.append("buying_power_unavailable")
    if not exit_auto:
        reasons.append("exit_automation_disabled")
    if not risk_safe:
        reasons.append("options_risk_limits_unsafe")
    if positions >= max_positions > 0:
        reasons.append("position_limit")
    if max_trades > 0 and trades >= max_trades:
        reasons.append("daily_trade_limit")
    if not runtime_entry_enabled:
        reasons.append("options_entries_disabled")
    route_active = not reasons
    return OptionsReadiness(
        user_id=user_id,
        environment=env,
        config_enabled=enabled,
        mode=mode,
        live_pilot_enabled=live_pilot,
        long_premium_only=long_only,
        broker_mode=broker_mode,
        broker_supported=broker_supported,
        runtime_entry_enabled=runtime_entry_enabled,
        market_data_ready=market_data_ready,
        option_chain_access=option_chain_access,
        broker_options_permission=broker_permission,
        buying_power_ready=buying_power_ready,
        exit_automation_ready=exit_auto,
        options_trade_executed_today=trade_executed_today,
        scanner_ready=scanner_ready,
        entry_router_ready=entry_router_ready,
        risk_ready=risk_safe,
        route_active=route_active,
        risk_limits_safe=risk_safe,
        positions_open=positions,
        max_positions=max_positions,
        trades_today=trades,
        max_trades_per_day=max_trades,
        max_contracts_per_trade=max_contracts,
        daily_limit_excluded=daily_usage.excluded,
        daily_limit_date=daily_usage.trading_date_et,
        final_status="ready" if route_active else "inactive",
        blocking_reasons=tuple(reasons),
    )


def format_startup_options_config(readiness: OptionsReadiness) -> str:
    reason = "" if readiness.final_status == "ready" else " reason=%s" % ",".join(readiness.blocking_reasons)
    return (
        "OPTIONS_CONFIG enabled=%s mode=%s live_pilot_enabled=%s long_premium_only=%s "
        "max_positions=%d max_trades_per_day=%d max_contracts_per_trade=%d broker_mode=%s "
        "broker_options_supported=%s final_status=%s%s"
        % (
            str(readiness.config_enabled).lower(),
            readiness.mode,
            str(readiness.live_pilot_enabled).lower(),
            str(readiness.long_premium_only).lower(),
            readiness.max_positions,
            readiness.max_trades_per_day,
            readiness.max_contracts_per_trade,
            readiness.broker_mode,
            str(readiness.broker_supported).lower(),
            "active" if readiness.final_status == "ready" else "inactive",
            reason,
        )
    )


def format_options_readiness(readiness: OptionsReadiness) -> list[str]:
    return [
        f"OPTIONS_READINESS user={readiness.user_id} environment={readiness.environment}",
        f"config_enabled={str(readiness.config_enabled).lower()}",
        f"mode={readiness.mode}",
        f"live_pilot_enabled={str(readiness.live_pilot_enabled).lower()}",
        f"runtime_entry_enabled={str(readiness.runtime_entry_enabled).lower()}",
        f"broker_supported={str(readiness.broker_supported).lower()}",
        f"market_data_ready={str(readiness.market_data_ready).lower()}",
        f"option_chain_access={str(readiness.option_chain_access).lower()}",
        f"broker_options_permission={str(readiness.broker_options_permission).lower()}",
        f"buying_power_ready={str(readiness.buying_power_ready).lower()}",
        f"exit_automation_ready={str(readiness.exit_automation_ready).lower()}",
        f"options_trade_executed_today={str(readiness.options_trade_executed_today).lower()}",
        f"scanner_ready={str(readiness.scanner_ready).lower()}",
        f"entry_router_ready={str(readiness.entry_router_ready).lower()}",
        f"risk_ready={str(readiness.risk_ready).lower()}",
        f"positions={readiness.positions_open}/{readiness.max_positions}",
        f"trades_today={readiness.trades_today}/{readiness.max_trades_per_day}",
        "OPTIONS_DAILY_LIMIT_USAGE limit=%d counted=%d excluded=%d date=%s timezone=America/New_York"
        % (
            readiness.max_trades_per_day,
            readiness.trades_today,
            readiness.daily_limit_excluded,
            readiness.daily_limit_date,
        ),
        f"final_status={'ready' if readiness.final_status == 'ready' else 'not_ready'}",
        f"blocking_reasons={','.join(readiness.blocking_reasons) if readiness.blocking_reasons else 'none'}",
    ]
