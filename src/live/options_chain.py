"""Option chain window helper for live-loop options routing."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any
import logging

import pytz

from src.options_config import live_options_explicitly_enabled, options_enabled, options_live_pilot_enabled

log = logging.getLogger(__name__)

_loop_non_paper_logged: set[str] = set()
_KNOWN_NON_OPTIONABLE_UNDERLYINGS = {
    "BTCUSD",
    "ETHUSD",
    "BTCUSDT",
    "ETHUSDT",
}


def broker_mode_is_paper(broker: Any, config: dict | None = None) -> bool:
    """True only when the active broker/config resolves to paper mode."""
    raw = getattr(broker, "paper", None)
    if raw is not None:
        return bool(raw)
    broker_cfg = (config or {}).get("broker") if isinstance(config, dict) else {}
    if isinstance(broker_cfg, dict) and "paper" in broker_cfg:
        return bool(broker_cfg.get("paper"))
    return True


def broker_mode(broker: Any, config: dict | None = None) -> str:
    """``paper`` or ``live`` for the active broker session."""
    return "paper" if broker_mode_is_paper(broker, config) else "live"


def options_runtime_enabled(broker: Any, config: dict | None = None) -> bool:
    """
    Options may run when enabled in config and either the broker is paper or the
    dedicated live pilot is explicitly enabled.

    Live mode is intentionally gated by ``options.live_pilot_enabled``.
    """
    cfg = config if isinstance(config, dict) else {}
    if not options_enabled(cfg):
        return False
    if broker_mode_is_paper(broker, cfg):
        return True
    return bool(live_options_explicitly_enabled(cfg) and options_live_pilot_enabled(cfg))


def reset_options_non_paper_log_flags() -> None:
    """Clear per-loop ``OPTIONS_DISABLED_NON_PAPER_MODE`` dedupe (call once each live cycle tick)."""
    _loop_non_paper_logged.clear()


def log_options_disabled_non_paper_once(user_id: str, broker: Any, config: dict | None = None) -> None:
    """Log ``OPTIONS_DISABLED_NON_PAPER_MODE`` at most once per user per live-loop iteration."""
    uid = str(user_id or "default").strip() or "default"
    if uid in _loop_non_paper_logged:
        return
    cfg = config if isinstance(config, dict) else {}
    opts = cfg.get("options") or {}
    if not bool(opts.get("enabled")):
        return
    if broker_mode_is_paper(broker, cfg):
        return
    if options_runtime_enabled(broker, cfg):
        return
    log_options_disabled_non_paper()
    _loop_non_paper_logged.add(uid)


def log_options_disabled_non_paper() -> None:
    log.info("OPTIONS_DISABLED_NON_PAPER_MODE")


def option_chain_expiry_bounds(config: dict, as_of: date) -> tuple[date, date]:
    cs = (config.get("options") or {}).get("contract_selection") or {}
    dte_min = int(cs.get("expiry_min_days", 14))
    dte_max = int(cs.get("expiry_max_days", 35))
    return as_of + timedelta(days=dte_min), as_of + timedelta(days=dte_max)


def is_optionable_underlying_symbol(symbol: str) -> bool:
    """Return false for synthetic/non-equity underlyings that Alpaca options cannot query."""
    sym = str(symbol or "").strip().upper()
    if not sym:
        return False
    if sym in _KNOWN_NON_OPTIONABLE_UNDERLYINGS:
        return False
    if "/" in sym or "-" in sym or "_" in sym:
        return False
    if sym.endswith("USD") and len(sym) > 5:
        return False
    import re

    return bool(re.fullmatch(r"[A-Z]{1,5}|[A-Z]{1,4}\.[A-Z]", sym))


def option_chain_for_underlying(
    broker: Any,
    config: dict,
    underlying: str,
    log_dt: datetime,
) -> list:
    """Alpaca option chain mapped to selector candidates (empty list on error / no client)."""
    if not options_runtime_enabled(broker, config):
        return []
    underlying_symbol = str(underlying or "").strip().upper()
    if not is_optionable_underlying_symbol(underlying_symbol):
        log.info("OPTIONS_SKIP symbol=%s reason=not_optionable_underlying", underlying_symbol or "?")
        return []
    et = pytz.timezone("America/New_York")
    as_of = log_dt.astimezone(et).date()
    lo, hi = option_chain_expiry_bounds(config, as_of)
    fn = getattr(broker, "get_option_chain_candidates", None)
    if fn is None:
        return []
    return fn(underlying_symbol, expiration_date_gte=lo, expiration_date_lte=hi)
