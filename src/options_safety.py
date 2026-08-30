"""Paper-options stabilization safety gates and promotion reporting helpers."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from src.options_config import max_bid_ask_spread_pct_cap, max_open_option_positions_cap
from src.options_performance import summarize_options_performance
from src.options_position_manager import _load_state
from src.options_selector import parse_occ_equity_option_symbol

log = logging.getLogger(__name__)

_KV_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=(\S+)")


def _opts(config: Mapping[str, Any] | None) -> Mapping[str, Any]:
    o = (config or {}).get("options") if isinstance(config, Mapping) else {}
    return o if isinstance(o, Mapping) else {}


def _exits(config: Mapping[str, Any] | None) -> Mapping[str, Any]:
    ex = _opts(config).get("exits")
    return ex if isinstance(ex, Mapping) else {}


def _as_float(raw: Any, default: float = 0.0) -> float:
    if raw is None or str(raw).strip() == "":
        return default
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return default
    return v if v == v else default


def _as_int(raw: Any, default: int = 0) -> int:
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return default


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _dt(raw: Any) -> datetime | None:
    if raw is None or str(raw).strip() == "":
        return None
    if isinstance(raw, datetime):
        out = raw
    else:
        try:
            out = datetime.fromisoformat(str(raw))
        except Exception:
            return None
    if out.tzinfo is None:
        out = out.replace(tzinfo=timezone.utc)
    return out.astimezone(timezone.utc)


def _option_diag(contract: Any, *, symbol: str | None = None, reason: str = "rejected") -> dict[str, Any]:
    contract_symbol = str(symbol or _get(contract, "symbol") or "").strip().upper()
    parsed = parse_occ_equity_option_symbol(contract_symbol)
    underlying = parsed[0] if parsed else str(_get(contract, "underlying") or "").strip().upper()
    expiration = parsed[1] if parsed else _get(contract, "expiration")
    dte = _get(contract, "dte")
    if dte is None and expiration is not None:
        try:
            exp = expiration if hasattr(expiration, "toordinal") else datetime.fromisoformat(str(expiration)).date()
            dte = max(0, (exp - datetime.now(timezone.utc).date()).days)
        except Exception:
            dte = None
    bid = _as_float(_get(contract, "bid"), -1.0)
    ask = _as_float(_get(contract, "ask"), -1.0)
    mid = (bid + ask) / 2.0 if bid > 0 and ask > 0 else 0.0
    spread_pct = abs(ask - bid) / mid * 100.0 if mid > 0 else 999.0
    return {
        "option_symbol": contract_symbol,
        "underlying": underlying,
        "reason": reason,
        "bid": bid,
        "ask": ask,
        "spread_pct": spread_pct,
        "volume": _as_int(_get(contract, "volume"), 0),
        "open_interest": _as_int(_get(contract, "open_interest"), 0),
        "delta": _get(contract, "delta"),
        "dte": dte,
    }


def _log_contract_reject(contract: Any, *, symbol: str | None = None, reason: str) -> None:
    d = _option_diag(contract, symbol=symbol, reason=reason)
    log.info(
        "OPTIONS_CONTRACT_REJECT option_symbol=%s symbol=%s underlying=%s reason=%s bid=%s ask=%s spread_pct=%.2f volume=%d open_interest=%d delta=%s dte=%s",
        d["option_symbol"],
        d["option_symbol"],
        d["underlying"] or "n/a",
        d["reason"],
        d["bid"],
        d["ask"],
        float(d["spread_pct"]),
        int(d["volume"]),
        int(d["open_interest"]),
        "n/a" if d["delta"] is None else d["delta"],
        "n/a" if d["dte"] is None else d["dte"],
    )


@dataclass(frozen=True)
class OptionsGateDecision:
    allowed: bool
    reason: str | None = None


def max_contracts_per_day(config: Mapping[str, Any] | None) -> int:
    """Daily contract cap. Zero means no explicit cap."""
    o = _opts(config)
    for key in ("max_option_contracts_per_day", "max_contracts_per_day"):
        cap = _as_int(o.get(key), 0)
        if cap > 0:
            return cap
    return 0


def max_daily_options_loss_dollars(config: Mapping[str, Any] | None) -> float:
    """Absolute daily options loss cap in dollars. Zero means no explicit dollar cap."""
    o = _opts(config)
    for key in ("max_daily_options_loss_dollars", "max_daily_loss_dollars"):
        cap = _as_float(o.get(key), 0.0)
        if cap > 0:
            return cap
    return 0.0


def max_daily_options_loss_percent(config: Mapping[str, Any] | None) -> float:
    """Daily options loss cap as percent of equity. Zero means no explicit percent cap."""
    o = _opts(config)
    for key in ("max_daily_options_loss_percent", "max_daily_options_loss_pct", "max_daily_loss_pct"):
        cap = _as_float(o.get(key), 0.0)
        if cap > 0:
            return cap
    return 0.0


def evaluate_options_daily_risk(
    config: Mapping[str, Any] | None,
    *,
    daily_realized_pl: float = 0.0,
    daily_unrealized_pl: float = 0.0,
    equity: float | None = None,
    contracts_opened_today: int = 0,
    open_option_positions: int = 0,
    new_contracts: int = 1,
    symbol: str | None = None,
) -> OptionsGateDecision:
    """Return whether a new option entry is allowed under daily paper safety gates."""
    total_pl = float(daily_realized_pl or 0.0) + float(daily_unrealized_pl or 0.0)
    dollar_cap = max_daily_options_loss_dollars(config)
    if dollar_cap > 0 and total_pl <= -dollar_cap + 1e-9:
        reason = "daily_loss_dollars %.2f <= -%.2f" % (total_pl, dollar_cap)
        log.info("OPTIONS_DAILY_RISK_BLOCK symbol=%s reason=%s", symbol or "n/a", reason)
        return OptionsGateDecision(False, reason)

    pct_cap = max_daily_options_loss_percent(config)
    if pct_cap > 0 and equity is not None and float(equity) > 0:
        pct_loss = abs(float(equity)) * (pct_cap / 100.0)
        if total_pl <= -pct_loss + 1e-9:
            reason = "daily_loss_percent %.2f <= -%.2f%% equity" % (total_pl, pct_cap)
            log.info("OPTIONS_DAILY_RISK_BLOCK symbol=%s reason=%s", symbol or "n/a", reason)
            return OptionsGateDecision(False, reason)

    day_cap = max_contracts_per_day(config)
    if day_cap > 0 and int(contracts_opened_today) + int(new_contracts) > day_cap:
        reason = "max_contracts_per_day %d + %d > %d" % (
            int(contracts_opened_today),
            int(new_contracts),
            day_cap,
        )
        log.info("OPTIONS_DAILY_RISK_BLOCK symbol=%s reason=%s", symbol or "n/a", reason)
        return OptionsGateDecision(False, reason)

    pos_cap = max_open_option_positions_cap(dict(config or {}))
    if pos_cap > 0 and int(open_option_positions) >= pos_cap:
        reason = "max_option_positions %d >= %d" % (int(open_option_positions), pos_cap)
        log.info("OPTIONS_DAILY_RISK_BLOCK symbol=%s reason=%s", symbol or "n/a", reason)
        return OptionsGateDecision(False, reason)

    return OptionsGateDecision(True, None)


def evaluate_option_contract_liquidity(
    config: Mapping[str, Any] | None,
    contract: Any,
    *,
    now: datetime | None = None,
    symbol: str | None = None,
) -> OptionsGateDecision:
    """Hard option-contract liquidity/quote gates with detailed reject logging."""
    bid = _as_float(_get(contract, "bid"), -1.0)
    ask = _as_float(_get(contract, "ask"), -1.0)
    contract_symbol = str(symbol or _get(contract, "symbol") or "").strip().upper()
    if bid <= 0 or ask <= 0:
        reason = "missing_quote"
        _log_contract_reject(contract, symbol=contract_symbol, reason=reason)
        return OptionsGateDecision(False, reason)
    if ask < bid:
        reason = "bad_quote"
        _log_contract_reject(contract, symbol=contract_symbol, reason=reason)
        return OptionsGateDecision(False, reason)

    ts = _dt(_get(contract, "timestamp"))
    max_age = _as_float(_opts(config).get("quote_stale_max_age_seconds"), _as_float(_opts(config).get("data_stale_max_age_seconds"), 120.0))
    if ts is not None:
        n = now or datetime.now(timezone.utc)
        if n.tzinfo is None:
            n = n.replace(tzinfo=timezone.utc)
        age = (n.astimezone(timezone.utc) - ts).total_seconds()
        if age > max_age:
            reason = "stale_quote"
            _log_contract_reject(contract, symbol=contract_symbol, reason=reason)
            log.info(
                "OPTIONS_CONTRACT_REJECT_DETAIL option_symbol=%s reason=%s quote_age_seconds=%.1f max_age_seconds=%.1f",
                contract_symbol,
                reason,
                age,
                max_age,
            )
            return OptionsGateDecision(False, reason)
    elif bool(_opts(config).get("require_quote_timestamp", False)):
        reason = "missing_quote_timestamp"
        _log_contract_reject(contract, symbol=contract_symbol, reason=reason)
        return OptionsGateDecision(False, reason)

    mid = (bid + ask) / 2.0
    spread_pct = abs(ask - bid) / mid * 100.0 if mid > 0 else 999.0
    max_spread = max_bid_ask_spread_pct_cap(dict(config or {}))
    if spread_pct > max_spread:
        reason = "wide_spread"
        _log_contract_reject(contract, symbol=contract_symbol, reason=reason)
        log.info(
            "OPTIONS_CONTRACT_REJECT_DETAIL option_symbol=%s reason=%s spread_pct=%.2f max_spread_pct=%.2f",
            contract_symbol,
            reason,
            spread_pct,
            max_spread,
        )
        return OptionsGateDecision(False, reason)

    cs = _opts(config).get("contract_selection")
    cs = cs if isinstance(cs, Mapping) else {}
    min_vol = _as_int(_opts(config).get("min_option_volume"), _as_int(cs.get("min_volume"), 0))
    volume = _as_int(_get(contract, "volume"), 0)
    if min_vol > 0 and volume < min_vol:
        reason = "low_volume"
        _log_contract_reject(contract, symbol=contract_symbol, reason=reason)
        log.info(
            "OPTIONS_CONTRACT_REJECT_DETAIL option_symbol=%s reason=%s volume=%d min_volume=%d",
            contract_symbol,
            reason,
            volume,
            min_vol,
        )
        return OptionsGateDecision(False, reason)

    min_oi = _as_int(_opts(config).get("min_open_interest"), _as_int(cs.get("min_open_interest"), 0))
    oi = _as_int(_get(contract, "open_interest"), 0)
    if min_oi > 0 and oi < min_oi:
        reason = "low_open_interest"
        _log_contract_reject(contract, symbol=contract_symbol, reason=reason)
        log.info(
            "OPTIONS_CONTRACT_REJECT_DETAIL option_symbol=%s reason=%s open_interest=%d min_open_interest=%d",
            contract_symbol,
            reason,
            oi,
            min_oi,
        )
        return OptionsGateDecision(False, reason)

    return OptionsGateDecision(True, None)


@dataclass(frozen=True)
class OptionLifecycleDecision:
    should_exit: bool
    reason: str | None = None


def evaluate_option_lifecycle_exit(
    config: Mapping[str, Any] | None,
    *,
    entry_time: datetime,
    now: datetime,
    pnl_pct: float | None = None,
    market_close_time: time = time(16, 0),
) -> OptionLifecycleDecision:
    """Lifecycle exits: take profit, stop loss, max hold minutes, and no overnight default."""
    ex = _exits(config)
    if not bool(ex.get("automation_enabled", True)):
        return OptionLifecycleDecision(False, None)
    if pnl_pct is not None:
        stop = _as_float(ex.get("stop_loss_pct"), 20.0)
        if stop > 0 and float(pnl_pct) <= -stop:
            return OptionLifecycleDecision(True, "option_stop_loss")
        take = _as_float(ex.get("take_profit_pct"), _as_float(ex.get("profit_take_pct"), 40.0))
        if take > 0 and float(pnl_pct) >= take:
            return OptionLifecycleDecision(True, "option_take_profit")
    if entry_time.tzinfo is None:
        entry_time = entry_time.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    held_min = (now.astimezone(timezone.utc) - entry_time.astimezone(timezone.utc)).total_seconds() / 60.0
    max_hold = _as_float(ex.get("max_hold_minutes"), 0.0)
    if max_hold > 0 and held_min >= max_hold:
        return OptionLifecycleDecision(True, "option_max_hold_minutes")
    allow_overnight = bool(ex.get("allow_overnight", False))
    force_min = _as_float(ex.get("force_close_minutes_before_close"), 15.0)
    close_dt = now.replace(
        hour=market_close_time.hour,
        minute=market_close_time.minute,
        second=0,
        microsecond=0,
    )
    if not allow_overnight and now >= close_dt - timedelta(minutes=force_min):
        return OptionLifecycleDecision(True, "option_no_overnight")
    return OptionLifecycleDecision(False, None)


def option_underlying_signal_allowed(signal: Any) -> OptionsGateDecision:
    """Options cannot be standalone: require underlying stock route/risk gates to have passed."""
    if signal is None:
        return OptionsGateDecision(False, "missing_underlying_signal")
    final = _get(signal, "final", _get(signal, "entry_eval_final", _get(signal, "decision_allowed")))
    if final is False:
        return OptionsGateDecision(False, "underlying_signal_not_final")
    if final is None and _get(signal, "passed") is not True:
        return OptionsGateDecision(False, "underlying_signal_missing_pass")
    if _get(signal, "risk_allowed", True) is False or _get(signal, "risk_gate_passed", True) is False:
        return OptionsGateDecision(False, "underlying_risk_gate_failed")
    if _get(signal, "route", None) is None and _get(signal, "source", None) is None:
        return OptionsGateDecision(False, "underlying_route_missing")
    return OptionsGateDecision(True, None)


def _parse_log_fields(line: str) -> dict[str, str]:
    return {m.group(1): m.group(2).strip('"') for m in _KV_RE.finditer(str(line or ""))}


def _review_log_paths(data_dir: Path | None, day: str) -> list[Path]:
    root = data_dir or (Path(__file__).resolve().parent.parent / "data")
    candidates = [
        root / "review" / day / "paper_full.log",
        root / "review" / day / "live_full.log",
    ]
    return [path for path in candidates if path.exists() and path.is_file()]


def _options_chain_diagnostics_from_logs(data_dir: Path | None, day: str) -> dict[str, Any]:
    chain_evaluations: list[dict[str, Any]] = []
    selected_contracts: list[dict[str, Any]] = []
    near_misses: list[dict[str, Any]] = []
    rejection_summary: dict[str, int] = {}
    orders_submitted = 0
    orders_filled = 0

    for path in _review_log_paths(data_dir, day):
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            if "OPTIONS_CHAIN_SUMMARY" in line:
                fields = _parse_log_fields(line)
                fields["log_path"] = str(path)
                chain_evaluations.append(fields)
                reason = fields.get("top_rejection_reason")
                if reason and reason != "none":
                    rejection_summary[reason] = rejection_summary.get(reason, 0) + 1
            elif "OPTION_SELECTED" in line:
                fields = _parse_log_fields(line)
                fields["log_path"] = str(path)
                selected_contracts.append(fields)
            elif "OPTION_NEAR_MISS" in line:
                fields = _parse_log_fields(line)
                fields["log_path"] = str(path)
                near_misses.append(fields)
                reason = fields.get("rejection_reason")
                if reason:
                    rejection_summary[reason] = rejection_summary.get(reason, 0) + 1
            elif "OPTION_CANDIDATE_REJECT" in line or "OPTIONS_CONTRACT_REJECT" in line:
                fields = _parse_log_fields(line)
                reason = fields.get("rejection_reason") or fields.get("reason")
                if reason:
                    rejection_summary[reason] = rejection_summary.get(reason, 0) + 1
            if "OPTION_ORDER_SUBMITTED" in line or ("ORDER_SUBMITTED" in line and "option" in line.lower()):
                orders_submitted += 1
            if "OPTION_ORDER_FILLED" in line or ("ORDER_FILLED" in line and "option" in line.lower()):
                orders_filled += 1

    return {
        "chain_evaluations": chain_evaluations,
        "selected_contracts": selected_contracts,
        "near_misses": near_misses,
        "rejection_summary": dict(sorted(rejection_summary.items())),
        "orders_submitted": orders_submitted,
        "orders_filled": orders_filled,
    }


def build_options_daily_report(
    *,
    user_id: str = "default",
    data_dir: Path | None = None,
    day: str | None = None,
) -> dict[str, Any]:
    """Return daily paper-option lifecycle report from persisted options state."""
    state = _load_state(user_id, data_dir=data_dir)
    day_key = day or datetime.now(timezone.utc).date().isoformat()
    rows: list[dict[str, Any]] = []
    for row in state.get("history", []) or []:
        if not isinstance(row, Mapping):
            continue
        entry_time = str(row.get("entry_time") or "")
        exit_time = str(row.get("exit_time") or "")
        if day and not (entry_time.startswith(day_key) or exit_time.startswith(day_key)):
            continue
        symbol = str(row.get("symbol") or "").strip().upper()
        parsed = parse_occ_equity_option_symbol(symbol)
        underlying, expiration, right, strike = parsed if parsed else (row.get("underlying"), None, row.get("right"), row.get("strike"))
        dte = None
        if expiration is not None and entry_time:
            et = _dt(entry_time)
            if et is not None:
                dte = max(0, (expiration - et.date()).days)
        entry_dt = _dt(entry_time)
        exit_dt = _dt(exit_time)
        hold_duration_minutes = None
        if entry_dt is not None and exit_dt is not None:
            hold_duration_minutes = max(0.0, (exit_dt - entry_dt).total_seconds() / 60.0)
        rows.append(
            {
                "underlying": underlying,
                "option_symbol": symbol,
                "call_put": right,
                "strike": strike,
                "expiration": None if expiration is None else expiration.isoformat(),
                "dte": dte if dte is not None else row.get("dte"),
                "entry_time": row.get("entry_time"),
                "entry_price": row.get("entry_fill_price", row.get("entry_price")),
                "exit_time": row.get("exit_time"),
                "exit_price": row.get("exit_price"),
                "quantity": row.get("qty", row.get("contracts")),
                "contracts": row.get("contracts", row.get("qty")),
                "bid_ask_spread_at_entry": row.get("entry_quote_spread_pct", row.get("quote_spread_pct")),
                "spread_at_entry": row.get("entry_quote_spread_pct", row.get("quote_spread_pct")),
                "volume": row.get("volume"),
                "open_interest": row.get("open_interest"),
                "entry_reason": row.get("entry_reason"),
                "exit_reason": row.get("exit_reason"),
                "realized_pnl": row.get("realized_pl"),
                "unrealized_pnl": row.get("unrealized_pl"),
                "hold_duration_minutes": hold_duration_minutes,
                "hold_duration": None if hold_duration_minutes is None else "%.1f minutes" % hold_duration_minutes,
                "missing_exit": not bool(row.get("exit_time")),
            }
        )
    open_positions = [
        dict(v) for v in (state.get("positions") or {}).values()
        if isinstance(v, Mapping) and str(v.get("status") or "").lower() == "open"
    ]
    chain_diagnostics = _options_chain_diagnostics_from_logs(data_dir, day_key)
    return {
        "user_id": user_id,
        "date": day_key,
        "trades": rows,
        "chain_evaluations": chain_diagnostics["chain_evaluations"],
        "selected_contracts": chain_diagnostics["selected_contracts"],
        "near_misses": chain_diagnostics["near_misses"],
        "rejection_summary": chain_diagnostics["rejection_summary"],
        "orders_submitted": chain_diagnostics["orders_submitted"] or len(rows),
        "orders_filled": chain_diagnostics["orders_filled"] or sum(1 for row in rows if row.get("entry_price") is not None),
        "missing_exits": sum(1 for row in rows if row.get("missing_exit")),
        "stuck_positions": len(open_positions),
        "open_positions": open_positions,
        "daily": (state.get("daily") or {}).get(day_key, {}),
    }


def _largest_trade(rows: list[dict[str, Any]], *, winner: bool) -> dict[str, Any] | None:
    valued = []
    for row in rows:
        pnl = _as_float(row.get("realized_pnl"), 0.0)
        valued.append((pnl, row))
    if not valued:
        return None
    pnl, row = max(valued, key=lambda x: x[0]) if winner else min(valued, key=lambda x: x[0])
    return {
        "option_symbol": row.get("option_symbol"),
        "underlying": row.get("underlying"),
        "realized_pnl": pnl,
        "exit_reason": row.get("exit_reason"),
    }


def build_options_promotion_report(
    config: Mapping[str, Any] | None = None,
    *,
    user_id: str = "default",
    data_dir: Path | None = None,
) -> dict[str, Any]:
    """Report-only live promotion criteria. Never enables live options."""
    summary = summarize_options_performance(user_id=user_id, data_dir=data_dir)
    daily_report = build_options_daily_report(user_id=user_id, data_dir=data_dir)
    o = _opts(config)
    min_trades = _as_int(o.get("promotion_min_trades"), 30)
    if min_trades <= 0:
        min_trades = 30
    pf_min = _as_float(o.get("promotion_min_profit_factor"), 1.3)
    live_min_trades = _as_int(o.get("promotion_live_review_min_trades"), 100)
    live_pf_min = _as_float(o.get("promotion_live_review_min_profit_factor"), 1.5)
    live_win_rate_min = _as_float(o.get("promotion_live_review_min_win_rate"), 50.0)
    reasons: list[str] = []
    if summary.total_trades < min_trades:
        reasons.append("total paper option trades %d < %d" % (summary.total_trades, min_trades))
    if summary.avg_trade_pnl <= 0:
        reasons.append("expectancy %.2f <= 0" % summary.avg_trade_pnl)
    if daily_report["stuck_positions"] > 0:
        reasons.append("stuck positions %d > 0" % daily_report["stuck_positions"])
    if daily_report["missing_exits"] > 0:
        reasons.append("missing exits %d > 0" % daily_report["missing_exits"])
    if summary.last_5_sessions_kill_switch_days:
        reasons.append("daily risk breach observed")
    if summary.profit_factor < pf_min:
        reasons.append("profit factor %.2f < %.2f" % (summary.profit_factor, pf_min))
    lifecycle_failures = (
        int(daily_report["missing_exits"])
        + int(daily_report["stuck_positions"])
        + len(summary.last_5_sessions_kill_switch_days)
    )
    if reasons:
        verdict = "NOT_READY"
    elif (
        summary.total_trades >= live_min_trades
        and summary.profit_factor >= live_pf_min
        and summary.win_rate >= live_win_rate_min
        and lifecycle_failures == 0
    ):
        verdict = "READY_FOR_LIVE_REVIEW"
    else:
        verdict = "READY_FOR_EXTENDED_PAPER"
    return {
        "total_trades": summary.total_trades,
        "total_paper_option_trades": summary.total_trades,
        "win_rate": summary.win_rate,
        "avg_win": summary.avg_win_pct,
        "avg_loss": summary.avg_loss_pct,
        "profit_factor": summary.profit_factor,
        "max_drawdown": summary.max_drawdown_pct,
        "max_drawdown_largest_loss": summary.max_drawdown_pct,
        "largest_winner": _largest_trade(daily_report["trades"], winner=True),
        "largest_loser": _largest_trade(daily_report["trades"], winner=False),
        "average_spread_paid": summary.avg_entry_spread_pct,
        "average_hold_duration": summary.avg_hold_time_minutes,
        "missing_exits": daily_report["missing_exits"],
        "missing_exits_count": daily_report["missing_exits"],
        "stuck_positions": daily_report["stuck_positions"],
        "stuck_positions_count": daily_report["stuck_positions"],
        "lifecycle_failures": lifecycle_failures,
        "rejection_summary": {},
        "promotion_verdict": verdict,
        "promotion_pass": len(reasons) == 0,
        "promotion_reasons": reasons,
        "report_only": True,
        "live_options_enabled": False,
    }
