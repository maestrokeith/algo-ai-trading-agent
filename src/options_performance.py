"""Analytics and promotion gate for paper options performance."""
from __future__ import annotations

import json
import logging
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from src.options_position_manager import options_state_path
from src.options_premium_risk import is_option_symbol

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class OptionsPerformanceSummary:
    total_trades: int
    wins: int
    losses: int
    win_rate: float
    total_pnl: float
    avg_trade_pnl: float
    avg_win_pct: float
    avg_loss_pct: float
    profit_factor: float
    max_drawdown_pct: float
    avg_hold_time_minutes: float
    best_contract: str | None
    worst_contract: str | None
    best_symbol: str | None
    worst_symbol: str | None
    best_entry_reason: str | None
    worst_entry_reason: str | None
    exit_reason_breakdown: dict[str, int]
    avg_entry_spread_pct: float
    avg_entry_slippage_bps: float
    avg_entry_slippage_pct: float
    closed_trades: list[dict[str, Any]]
    last_5_sessions_kill_switch_days: list[str]


@dataclass(frozen=True)
class StockPerformanceSummary:
    total_trades: int
    wins: int
    losses: int
    win_rate: float
    total_pnl: float
    avg_trade_pnl: float
    avg_return_pct: float
    best_symbol: str | None
    worst_symbol: str | None


@dataclass(frozen=True)
class StockVsOptionsComparison:
    options: OptionsPerformanceSummary
    stocks: StockPerformanceSummary
    pnl_edge: str
    win_rate_edge: str


def _load_state(user_id: str = "default", *, data_dir: Path | None = None) -> dict[str, Any]:
    path = options_state_path(user_id, data_dir=data_dir)
    if not path.exists():
        return {"meta": {}, "positions": {}, "history": [], "daily": {}}
    try:
        with open(path) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {"meta": {}, "positions": {}, "history": [], "daily": {}}
        data.setdefault("meta", {})
        data.setdefault("positions", {})
        data.setdefault("history", [])
        data.setdefault("daily", {})
        return data
    except Exception:
        return {"meta": {}, "positions": {}, "history": [], "daily": {}}


def _as_float(raw: Any, default: float = 0.0) -> float:
    if raw is None or str(raw).strip() == "":
        return default
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return default
    if v != v:
        return default
    return v


def _as_dt(raw: Any) -> datetime | None:
    if raw is None or str(raw).strip() == "":
        return None
    try:
        dt = datetime.fromisoformat(str(raw))
        if getattr(dt, "tzinfo", None) is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _trade_pnl_pct(row: Mapping[str, Any]) -> float:
    realized = _as_float(row.get("realized_pl"), 0.0)
    premium = _as_float(row.get("premium_paid"), 0.0)
    if premium > 0:
        return (realized / premium) * 100.0
    entry_fill = _as_float(row.get("entry_fill_price"), 0.0)
    qty = max(1.0, _as_float(row.get("qty"), 1.0))
    if entry_fill > 0:
        basis = entry_fill * qty * 100.0
        return (realized / basis) * 100.0 if basis > 0 else 0.0
    return 0.0


def _row_pnl(row: Mapping[str, Any]) -> float:
    for key in ("pnl", "realized_pnl", "profit_loss", "realized_profit_loss", "realized_pl"):
        raw = row.get(key)
        if raw is not None and str(raw).strip() != "":
            return _as_float(raw, 0.0)
    return 0.0


def _row_return_pct(row: Mapping[str, Any], *, pnl: float) -> float | None:
    for key in ("return_pct", "pnl_pct", "realized_return_pct", "profit_loss_pct"):
        raw = row.get(key)
        if raw is not None and str(raw).strip() != "":
            return _as_float(raw, 0.0)
    qty = abs(_as_float(row.get("qty") if row.get("qty") is not None else row.get("filled_qty"), 0.0))
    price = _as_float(row.get("filled_avg_price"), 0.0)
    notional = qty * price
    if notional > 0 and pnl != 0.0:
        return (pnl / notional) * 100.0
    return None


def _trade_slippage_bps(row: Mapping[str, Any]) -> float | None:
    intended = _as_float(row.get("intended_limit_price"), 0.0)
    fill = _as_float(row.get("entry_fill_price"), 0.0)
    if intended <= 0 or fill <= 0:
        entry = _as_float(row.get("entry_price"), 0.0)
        if intended <= 0 or entry <= 0:
            return None
        fill = entry
    return (fill - intended) / intended * 10_000.0


def _hold_minutes(row: Mapping[str, Any]) -> float | None:
    et = _as_dt(row.get("entry_time"))
    xt = _as_dt(row.get("exit_time"))
    if et is None or xt is None:
        return None
    return max(0.0, (xt - et).total_seconds() / 60.0)


def load_closed_option_trades(user_id: str = "default", *, data_dir: Path | None = None) -> list[dict[str, Any]]:
    state = _load_state(user_id, data_dir=data_dir)
    rows = []
    for row in state.get("history", []) or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("exit_time") or "").strip() == "" and str(row.get("status") or "").lower() != "closed":
            continue
        sym = str(row.get("symbol") or "").strip().upper()
        if not sym:
            continue
        row = dict(row)
        row["pnl_pct"] = _trade_pnl_pct(row)
        row["slippage_bps"] = _trade_slippage_bps(row)
        row["hold_minutes"] = _hold_minutes(row)
        rows.append(row)
    rows.sort(key=lambda r: str(r.get("exit_time") or ""))
    return rows


def _aggregate_best_worst_label(values: dict[str, list[float]], *, mode: str = "avg") -> tuple[str | None, str | None]:
    if not values:
        return None, None
    scored = []
    for k, vals in values.items():
        if not vals:
            continue
        score = sum(vals) / len(vals) if mode == "avg" else sum(vals)
        scored.append((score, k))
    if not scored:
        return None, None
    scored.sort()
    worst = scored[0][1]
    best = scored[-1][1]
    return best, worst


def summarize_options_performance(user_id: str = "default", *, data_dir: Path | None = None) -> OptionsPerformanceSummary:
    trades = load_closed_option_trades(user_id, data_dir=data_dir)
    total = len(trades)
    wins = [t for t in trades if _as_float(t.get("realized_pl"), 0.0) > 0]
    losses = [t for t in trades if _as_float(t.get("realized_pl"), 0.0) < 0]
    win_rate = (len(wins) / total * 100.0) if total > 0 else 0.0
    total_pnl = sum(_as_float(t.get("realized_pl"), 0.0) for t in trades)
    avg_trade_pnl = total_pnl / total if total > 0 else 0.0
    avg_win_pct = sum(_trade_pnl_pct(t) for t in wins) / len(wins) if wins else 0.0
    avg_loss_pct = sum(_trade_pnl_pct(t) for t in losses) / len(losses) if losses else 0.0
    gross_profit = sum(_as_float(t.get("realized_pl"), 0.0) for t in wins)
    gross_loss = abs(sum(_as_float(t.get("realized_pl"), 0.0) for t in losses))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else (float("inf") if gross_profit > 0 else 0.0)

    cumulative_pnl = 0.0
    peak_pnl = 0.0
    max_dd_abs = 0.0
    total_premium = sum(_as_float(t.get("premium_paid"), 0.0) for t in trades)
    for t in sorted(trades, key=lambda r: str(r.get("exit_time") or "")):
        cumulative_pnl += _as_float(t.get("realized_pl"), 0.0)
        peak_pnl = max(peak_pnl, cumulative_pnl)
        max_dd_abs = max(max_dd_abs, peak_pnl - cumulative_pnl)
    max_dd = (max_dd_abs / total_premium * 100.0) if total_premium > 0 else 0.0

    hold_minutes = [t.get("hold_minutes") for t in trades if t.get("hold_minutes") is not None]
    avg_hold = sum(float(v) for v in hold_minutes) / len(hold_minutes) if hold_minutes else 0.0
    symbol_pnl: dict[str, list[float]] = defaultdict(list)
    reason_pnl: dict[str, list[float]] = defaultdict(list)
    exit_reason_counts: Counter[str] = Counter()
    spreads: list[float] = []
    slippage_bps: list[float] = []
    for t in trades:
        sym = str(t.get("symbol") or "").strip().upper()
        if sym:
            symbol_pnl[sym].append(_as_float(t.get("realized_pl"), 0.0))
        reason = str(t.get("entry_reason") or "unknown").strip() or "unknown"
        reason_pnl[reason].append(_trade_pnl_pct(t))
        exit_reason_counts[str(t.get("exit_reason") or "unknown").strip() or "unknown"] += 1
        sp = t.get("entry_quote_spread_pct")
        if sp is None:
            sp = t.get("quote_spread_pct")
        if sp is not None:
            spreads.append(_as_float(sp, 0.0))
        sl = t.get("slippage_bps")
        if sl is not None:
            slippage_bps.append(_as_float(sl, 0.0))

    best_symbol, worst_symbol = _aggregate_best_worst_label(symbol_pnl, mode="sum")
    best_reason, worst_reason = _aggregate_best_worst_label(reason_pnl, mode="avg")
    avg_spread = sum(spreads) / len(spreads) if spreads else 0.0
    avg_slippage_bps = sum(slippage_bps) / len(slippage_bps) if slippage_bps else 0.0
    avg_slippage_pct = avg_slippage_bps / 100.0

    state = _load_state(user_id, data_dir=data_dir)
    daily = state.get("daily", {})
    sessions = []
    if isinstance(daily, dict):
        for day, row in daily.items():
            if not isinstance(row, dict):
                continue
            sessions.append(
                (
                    str(day),
                    bool(row.get("kill_switch_on"))
                    or bool(row.get("block_new_entries"))
                    or bool(row.get("options_kill_switch_on")),
                )
            )
    sessions.sort(key=lambda x: x[0])
    last_5 = sessions[-5:]
    kill_days = [day for day, on in last_5 if on]

    return OptionsPerformanceSummary(
        total_trades=total,
        wins=len(wins),
        losses=len(losses),
        win_rate=win_rate,
        total_pnl=total_pnl,
        avg_trade_pnl=avg_trade_pnl,
        avg_win_pct=avg_win_pct,
        avg_loss_pct=avg_loss_pct,
        profit_factor=profit_factor,
        max_drawdown_pct=max_dd,
        avg_hold_time_minutes=avg_hold,
        best_contract=best_symbol,
        worst_contract=worst_symbol,
        best_symbol=best_symbol,
        worst_symbol=worst_symbol,
        best_entry_reason=best_reason,
        worst_entry_reason=worst_reason,
        exit_reason_breakdown=dict(exit_reason_counts),
        avg_entry_spread_pct=avg_spread,
        avg_entry_slippage_bps=avg_slippage_bps,
        avg_entry_slippage_pct=avg_slippage_pct,
        closed_trades=trades,
        last_5_sessions_kill_switch_days=kill_days,
    )


def summarize_stock_trade_performance(trades: list[Mapping[str, Any]] | None) -> StockPerformanceSummary:
    """Summarize realized stock trade rows for stock-vs-option comparison."""
    rows: list[Mapping[str, Any]] = []
    for row in trades or []:
        if not isinstance(row, Mapping):
            continue
        symbol = str(row.get("symbol") or "").strip().upper()
        if not symbol or is_option_symbol(symbol):
            continue
        pnl = _row_pnl(row)
        has_explicit_pnl = any(
            row.get(k) is not None and str(row.get(k)).strip() != ""
            for k in ("pnl", "realized_pnl", "profit_loss", "realized_profit_loss", "realized_pl")
        )
        if not has_explicit_pnl and pnl == 0.0:
            continue
        rows.append(row)

    total = len(rows)
    pnl_values = [_row_pnl(row) for row in rows]
    wins = [p for p in pnl_values if p > 0]
    losses = [p for p in pnl_values if p < 0]
    total_pnl = sum(pnl_values)
    returns = [
        r
        for r in (_row_return_pct(row, pnl=_row_pnl(row)) for row in rows)
        if r is not None
    ]
    symbol_pnl: dict[str, float] = defaultdict(float)
    for row in rows:
        symbol_pnl[str(row.get("symbol") or "").strip().upper()] += _row_pnl(row)
    best_symbol = None
    worst_symbol = None
    if symbol_pnl:
        ranked = sorted((v, k) for k, v in symbol_pnl.items())
        worst_symbol = ranked[0][1]
        best_symbol = ranked[-1][1]
    return StockPerformanceSummary(
        total_trades=total,
        wins=len(wins),
        losses=len(losses),
        win_rate=(len(wins) / total * 100.0) if total > 0 else 0.0,
        total_pnl=total_pnl,
        avg_trade_pnl=total_pnl / total if total > 0 else 0.0,
        avg_return_pct=sum(returns) / len(returns) if returns else 0.0,
        best_symbol=best_symbol,
        worst_symbol=worst_symbol,
    )


def compare_stock_vs_options(
    options_summary: OptionsPerformanceSummary,
    stock_trades: list[Mapping[str, Any]] | None,
) -> StockVsOptionsComparison:
    """Compare realized stock and option performance."""
    stock_summary = summarize_stock_trade_performance(stock_trades)
    if options_summary.total_pnl > stock_summary.total_pnl:
        pnl_edge = "options"
    elif options_summary.total_pnl < stock_summary.total_pnl:
        pnl_edge = "stocks"
    else:
        pnl_edge = "tie"
    if options_summary.win_rate > stock_summary.win_rate:
        win_rate_edge = "options"
    elif options_summary.win_rate < stock_summary.win_rate:
        win_rate_edge = "stocks"
    else:
        win_rate_edge = "tie"
    return StockVsOptionsComparison(
        options=options_summary,
        stocks=stock_summary,
        pnl_edge=pnl_edge,
        win_rate_edge=win_rate_edge,
    )


def format_options_performance_dashboard(
    summary: OptionsPerformanceSummary,
    *,
    comparison: StockVsOptionsComparison | None = None,
) -> str:
    """Render a compact operator dashboard for paper options analytics."""
    lines = [
        "Options performance dashboard",
        (
            "Options: trades=%d pnl=$%.2f win_rate=%.2f%% avg_hold=%.2fmin "
            "best_contract=%s worst_contract=%s"
            % (
                summary.total_trades,
                summary.total_pnl,
                summary.win_rate,
                summary.avg_hold_time_minutes,
                summary.best_contract or "none",
                summary.worst_contract or "none",
            )
        ),
        (
            "Options detail: avg_trade_pnl=$%.2f avg_win=%.2f%% avg_loss=%.2f%% "
            "profit_factor=%s max_drawdown=%.2f%%"
            % (
                summary.avg_trade_pnl,
                summary.avg_win_pct,
                summary.avg_loss_pct,
                "inf" if summary.profit_factor == float("inf") else f"{summary.profit_factor:.2f}",
                summary.max_drawdown_pct,
            )
        ),
    ]
    if comparison is not None:
        stocks = comparison.stocks
        lines.append(
            "Stocks: trades=%d pnl=$%.2f win_rate=%.2f%% avg_return=%.2f%% best_symbol=%s worst_symbol=%s"
            % (
                stocks.total_trades,
                stocks.total_pnl,
                stocks.win_rate,
                stocks.avg_return_pct,
                stocks.best_symbol or "none",
                stocks.worst_symbol or "none",
            )
        )
        lines.append(
            "Stock vs option edge: pnl=%s win_rate=%s"
            % (comparison.pnl_edge, comparison.win_rate_edge)
        )
    return "\n".join(lines)


def evaluate_options_promotion_gate(summary: OptionsPerformanceSummary) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if summary.total_trades < 30:
        reasons.append(f"need at least 30 closed paper option trades (have {summary.total_trades})")
    if not (summary.profit_factor >= 1.3):
        pf = "inf" if summary.profit_factor == float("inf") else f"{summary.profit_factor:.2f}"
        reasons.append(f"profit factor {pf} < 1.30")
    if not (summary.max_drawdown_pct <= 3.0):
        reasons.append(f"max drawdown {summary.max_drawdown_pct:.2f}% > 3.00%")
    if summary.last_5_sessions_kill_switch_days:
        reasons.append(
            "kill-switch days in last 5 sessions: "
            + ",".join(summary.last_5_sessions_kill_switch_days)
        )
    if not (summary.avg_entry_spread_pct <= 8.0):
        reasons.append(f"average entry spread {summary.avg_entry_spread_pct:.2f}% > 8.00%")
    return (len(reasons) == 0), reasons
