"""
Execution rules: limit orders in liquid names, spread gate, partial fills, slippage tracking.

- **Spread-based equity routing** (optional ``market_order_if_spread_below_pct``): when set, if
  ``spread_pct <`` that threshold use **market** (buy: notional from qty×mid; sell: share qty); else
  **limit at mid** (rounded). When unset, equity buys/sells keep the legacy **always market** path
  (options still use ``prefer_limit_orders`` / inside-spread).

- **Smart limits** (default ``limit_price_mode: inside_spread``): buy at
  ``bid + inside_spread_fraction * (ask - bid)``, sell at
  ``ask - inside_spread_fraction * (ask - bid)`` when NBBO is known; else infer width from
  ``mid_price`` and ``spread_pct``.
- **Bucket cap relax** (``execution.allow_bucket_override_for_top_signals`` + ``top_signal_percentile``):
  ranked entry waves may pass :func:`execution_bucket_top_signal_qualified` so a per-bucket MV cap
  is not a hard block for the best ``strength_eff`` names (see :mod:`src.risk_limits`, live loop).
- **RFC guard** (``execution.disable_rebalance_if_strong_signals``): do not fund a new long by
  ``rebalance_free_capital`` sells when the incoming name is a **strong** signal; see
  :func:`execution_rebalance_deferred_because_incoming_strong` (``run_alpaca_loop``).
- **Full invest** (``execution.strong_signals_count`` + ``strong_signal_strength_min``): when at
  least *N* entry rows in the same scan have ``strength_eff`` above the floor, live loop sets
  :func:`src.portfolio_allocation.effective_buying_power_for_entries` *full_invest* so the cash
  reserve is not held back (see that module).
- **Post-buy sell cooldown** (``execution.no_sell_within_min_of_buy``): skip discretionary stock
  sells and RFC trims for that long until the window elapses; see
  :func:`parse_no_sell_within_min_of_buy` and :class:`src.live.exits.LiveExitContext` ``post_buy_sell_cooldown_active``.
- **Post-exit re-entry wait** (``execution.symbol_cooldown_minutes`` / ``symbol_cooldown_etf_minutes`` /
  optional ``symbol_cooldown_top_momentum_symbols`` + ``symbol_cooldown_top_momentum_minutes``):
  in :meth:`src.trading_engine.TradingEngine.run_entry_gates`, do not re-buy until
  the stricter of ``entries.per_symbol_sell_cooldown_min`` and the execution tier (see
  :func:`symbol_post_exit_cooldown_minutes`) has elapsed since the last recorded stop/profit exit.
- Legacy ``mid_offset_ticks``: offset from mid by whole ticks (previous default behavior).
- Don't trade if spread too wide (``max_spread_pct`` scalar or tiers: ``large_caps``, ``mid_caps``,
  ``small_caps`` / ``volatile``; optional ``skip_if_spread_above_pct``). Optional
  ``retry_on_spread_fail`` + ``retry_attempts`` (see :meth:`build_order_for_entry`) re-run
  :meth:`build_order` with the spread gate ignored after a failed attempt (e.g. transient wide NBBO).
- **Equities (non-OCC symbols):** **buy** market orders use **notional** (USD) ≈ ``round(quantity * mid_price, 2)``
  when ``execution.allow_fractional`` is true (default); when false, buys use **whole-share** ``quantity``
  only (``notional=None``). **Sell** market orders always use whole-share ``quantity`` (no notional).
  OCC option symbols keep limit-or-market-by-**qty**.
- Optional ``execution.min_trade_dollars`` (``> 0``): reject equity **buy** market orders whose
  **notional** is strictly below that USD floor (explicit ``notional=`` or derived ``qty × mid``).
- Optional ``execution.min_order_notional`` (``> 0``): for **fractional** long probes only (e.g. live
  trend loop), minimum USD to compare against *available cash* before entry gates. Avoids a **1-share**
  floor when ``allow_fractional`` (alias ``allow_fractional_shares``) is true; the effective probe is
        ``max(min_order_notional, min_trade_dollars)`` when either is set, else **$1** (fractional only;
        non-fractional still uses the full per-share *mid* as the floor).
- Optional ``execution.min_sell_pct_of_position`` / ``min_trade_value``: when ``build_order(...,
  position_qty=…)`` is set for an equity **sell**, partial sells are bumped up to at least that
  fraction of the open position and/or ``ceil(min_trade_value / mid)`` shares (capped at a full exit).
- Explicit **notional**: ``build_order(..., notional=1000)`` or
  ``build_order_from_dict({"symbol": "SPY", "side": "buy", "notional": 1000}, ...)``.
- Handle partial fills + cancel/replace logic; track slippage vs ``expected_price`` (mid).
"""
from __future__ import annotations

import math
import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Iterable, Mapping

from .options_premium_risk import is_option_symbol
from src.universe import liquid_spread_relief_parse

log = logging.getLogger(__name__)


def _parse_exposure_fraction(value: Any) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v) or v <= 0:
        return None
    if v > 1.0:
        v = v / 100.0
    return max(0.0, min(5.0, v))


def _order_build_float(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return float(default)
    return out if math.isfinite(out) else float(default)


def _log_order_build_reject(
    *,
    symbol: Any,
    reason: str,
    route: Any = None,
    source: Any = None,
    notional: Any = None,
    qty: Any = None,
) -> None:
    log.info(
        "ORDER_BUILD_REJECT symbol=%s reason=%s route=%s source=%s notional=%.2f qty=%.4f",
        str(symbol or "?").strip().upper() or "?",
        str(reason or "unknown"),
        str(route or "n/a"),
        str(source or "n/a"),
        _order_build_float(notional, 0.0),
        _order_build_float(qty, 0.0),
    )


def execution_bucket_top_signal_qualified(
    config: dict[str, Any] | None,
    *,
    strength: float,
    strength_cohort: list[float] | None,
) -> bool:
    """
    When ``execution.allow_bucket_override_for_top_signals`` is true, return whether *strength* is
    in the top ``execution.top_signal_percentile`` fraction of *strength_cohort* (by ``strength_eff``,
    latest-highest first). Requires at least two values in the cohort; single-name waves do not
    qualify (avoids treating one candidate as "always in the top 20%").
    """
    ex = (config or {}).get("execution")
    ex = ex if isinstance(ex, dict) else {}
    if not bool(ex.get("allow_bucket_override_for_top_signals", False)):
        return False
    try:
        p = float(ex.get("top_signal_percentile", 0.2))
    except (TypeError, ValueError):
        p = 0.2
    if p <= 0.0 or p > 1.0:
        p = 0.2
    vals: list[float] = []
    for s in strength_cohort or []:
        try:
            f = float(s)
        except (TypeError, ValueError):
            continue
        if f == f:
            vals.append(f)
    try:
        s0 = float(strength)
    except (TypeError, ValueError):
        return False
    if s0 != s0:
        return False
    if not any(abs(f - s0) < 1e-9 for f in vals):
        vals.append(s0)
    n = len(vals)
    if n < 2:
        return False
    desc = sorted(vals, reverse=True)
    k = max(1, int(math.ceil(n * p)))
    if k > n:
        k = n
    cutoff = desc[k - 1]
    return s0 + 1e-12 >= cutoff


def execution_rebalance_deferred_because_incoming_strong(
    config: dict[str, Any] | None,
    *,
    incoming_strength: float | None,
    strength_cohort: list[float] | None = None,
) -> bool:
    """
    When ``execution.disable_rebalance_if_strong_signals`` is true, return True to **skip** proactive
    rebalance-free-capital sells (funding a new long) while the incoming name is a **strong** signal.

    * With at least two float values in *strength_cohort*, *strong* means
      :func:`execution_bucket_top_signal_qualified` (top ``top_signal_percentile`` of the wave).
    * With fewer than two, *strong* means ``incoming_strength`` ≥
      ``execution.rebalance_incoming_strength_block_min`` (default ``0.85``) when the flag is on.

    *incoming_strength* of ``None``/NaN → not treated as strong (rebalance not deferred).
    """
    ex = (config or {}).get("execution")
    ex = ex if isinstance(ex, dict) else {}
    if not bool(ex.get("disable_rebalance_if_strong_signals", False)):
        return False
    if incoming_strength is None:
        return False
    try:
        s = float(incoming_strength)
    except (TypeError, ValueError):
        return False
    if s != s or s < 0.0:
        return False
    vals: list[float] = []
    for x in strength_cohort or []:
        try:
            f = float(x)
        except (TypeError, ValueError):
            continue
        if f == f:
            vals.append(f)
    if len(vals) >= 2 and bool(
        ex.get("allow_bucket_override_for_top_signals", False)
    ):
        return bool(
            execution_bucket_top_signal_qualified(
                config, strength=s, strength_cohort=vals
            )
        )
    try:
        s_min = float(
            ex.get("rebalance_incoming_strength_block_min", 0.85)
        )
    except (TypeError, ValueError):
        s_min = 0.85
    s_min = max(0.0, min(1.0, s_min))
    return s >= s_min - 1e-12


def parse_no_sell_within_min_of_buy(config: dict[str, Any] | None) -> float:
    """
    ``execution.no_sell_within_min_of_buy`` — minutes after a buy/scale (tracker ``entry_time`` /
    ``last_add_time`` / ``last_scale_ts``) during which **discretionary** stock sells are skipped
    in :mod:`src.live.exits` and ``rebalance_free_capital`` trims. ``0`` = off.
    """
    ex = (config or {}).get("execution")
    ex = ex if isinstance(ex, dict) else {}
    raw = ex.get("no_sell_within_min_of_buy", 0)
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, v)


# Default list when ``execution.symbol_cooldown_etf_symbols`` is omitted (higher re-entry wait).
_DEFAULT_SYMBOL_COOLDOWN_ETF_SYMBOLS: frozenset[str] = frozenset({"SPY", "XLP"})


def _execution_top_momentum_symbol_set(ex: Mapping[str, Any]) -> frozenset[str]:
    """Symbols that use ``symbol_cooldown_top_momentum_minutes`` for post-exit re-entry."""
    raw = ex.get("symbol_cooldown_top_momentum_symbols")
    if raw is None:
        raw = ex.get("top_momentum_symbols")
    if not isinstance(raw, (list, tuple, set)) or not raw:
        return frozenset()
    return frozenset(
        str(x).strip().upper() for x in raw if str(x).strip()
    )


def parse_per_symbol_sell_cooldown_min(entries_cfg: Mapping[str, Any] | None) -> float:
    """``entries.per_symbol_sell_cooldown_min`` (0 = off). Used by the engine and the live loop."""
    ec = dict(entries_cfg or {})
    raw = ec.get("per_symbol_sell_cooldown_min", 0)
    try:
        return float(raw) if raw is not None and str(raw).strip() != "" else 0.0
    except (TypeError, ValueError):
        return 0.0


def minutes_since_last_broker_structured_exit(
    state: Any,
    symbol_upper: str,
    now_dt: datetime,
) -> float | None:
    """
    Wall-clock minutes since the latest of ``last_stop_loss_at`` / ``last_profit_exit_at`` for
    *symbol_upper* (same source as :func:`minutes_since_last_recorded_exit` in ``loop_helpers``).
    """
    want = str(symbol_upper or "").strip().upper()
    candidates: list[datetime] = []
    for mapping in (getattr(state, "last_stop_loss_at", None), getattr(state, "last_profit_exit_at", None)):
        if not isinstance(mapping, dict):
            continue
        for k, v in mapping.items():
            if str(k).strip().upper() != want:
                continue
            candidates.append(v)
    if not candidates:
        return None
    last_exit = max(candidates)
    last_naive = last_exit.replace(tzinfo=None) if last_exit.tzinfo else last_exit
    n = now_dt.replace(tzinfo=None) if now_dt.tzinfo else now_dt
    return (n - last_naive).total_seconds() / 60.0


def symbol_post_exit_cooldown_minutes(
    symbol_upper: str,
    config: dict[str, Any] | None,
) -> float:
    """
    Re-entry wait (minutes) after a structured stop/profit record.

    When *symbol_upper* is in ``execution.symbol_cooldown_top_momentum_symbols`` (alias
    ``execution.top_momentum_symbols``), returns ``symbol_cooldown_top_momentum_minutes``
    (default ``25``, i.e. a 20–30 minute band) instead of the ETF vs stock tiers below — **checked
    first** so strategic “top momentum” names avoid a rigid long ETF wait.

    Otherwise:

    - ``execution.symbol_cooldown_etf_symbols`` lists names that use the ETF tier.
    - ``execution.symbol_cooldown_minutes`` — stocks; ``0`` = off.
    - ``execution.symbol_cooldown_etf_minutes`` — ETFs in the list; if the key is **omitted**,
      defaults to ``60``; explicit ``0`` = off for that tier.
    - The live loop stricter-combines with ``entries.per_symbol_sell_cooldown_min`` as ``max(...)``.
    """
    ex = (config or {}).get("execution")
    ex = ex if isinstance(ex, dict) else {}
    su = str(symbol_upper or "").strip().upper()
    try:
        _floor_raw = ex.get("min_recent_exit_reentry_minutes", ex.get("reentry_cooldown_minutes", 0))
        floor_minutes = (
            max(0.0, float(_floor_raw))
            if _floor_raw is not None and str(_floor_raw).strip() != ""
            else 75.0
        )
    except (TypeError, ValueError):
        floor_minutes = 75.0
    tm_set = _execution_top_momentum_symbol_set(ex)
    if tm_set and su in tm_set:
        tm_raw = ex.get("symbol_cooldown_top_momentum_minutes")
        if tm_raw is None or str(tm_raw).strip() == "":
            return max(25.0, floor_minutes)
        try:
            return max(0.0, float(tm_raw), floor_minutes)
        except (TypeError, ValueError):
            return max(25.0, floor_minutes)
    etf_list_raw = ex.get("symbol_cooldown_etf_symbols")
    if etf_list_raw is None or etf_list_raw is False:
        etf_set = set(_DEFAULT_SYMBOL_COOLDOWN_ETF_SYMBOLS)
    elif isinstance(etf_list_raw, (list, tuple, set)) and not etf_list_raw:
        etf_set: set[str] = set()  # explicit "no ETF tier"
    else:
        etf_set = {
            str(x).strip().upper() for x in (etf_list_raw or []) if str(x).strip()
        }
    if su in etf_set:
        if "symbol_cooldown_etf_minutes" not in ex:
            return max(60.0, floor_minutes)
        try:
            return max(0.0, float(ex.get("symbol_cooldown_etf_minutes", 0)), floor_minutes)
        except (TypeError, ValueError):
            return floor_minutes
    raw_s = ex.get("symbol_cooldown_minutes")
    try:
        v = float(raw_s) if raw_s is not None and str(raw_s).strip() != "" else 0.0
    except (TypeError, ValueError):
        return floor_minutes
    return max(0.0, v, floor_minutes)


def execution_bypass_no_sell_after_buy_cooldown(reason: Any) -> bool:
    """
    When True, a sell is allowed even if we are still inside ``no_sell_within_min_of_buy``:
    stop-like, kill-switch, and symbol-allocation cap rebalance.
    """
    v = reason.value if hasattr(reason, "value") else str(reason)
    s = str(v).strip().lower()
    if s in (
        "stop_loss",
        "option_stop_loss",
        "trailing_stop",
        "trail",
    ):
        return True
    if s in ("kill_switch", "kill_switch_partial"):
        return True
    if s == "risk_cap_rebalance":
        return True
    return False


def limit_price_inside_spread(
    side: str,
    *,
    bid: float | None,
    ask: float | None,
    mid_price: float,
    spread_pct: float,
    inside_fraction: float,
) -> float:
    """
    Passive-aggressive limit inside the spread.

    * **buy**: ``bid + f * (ask - bid)``
    * **sell**: ``ask - f * (ask - bid)`` with ``f`` clamped to ``[0, 1]``.

    If *bid* / *ask* are missing or invalid, assume a symmetric book:
    ``width = mid * spread_pct/100``, ``bid = mid - width/2``, ``ask = mid + width/2``.
    """
    f = max(0.0, min(1.0, float(inside_fraction)))
    sb = str(side or "").strip().lower()
    if (
        bid is not None
        and ask is not None
        and float(bid) > 0
        and float(ask) > 0
        and float(ask) >= float(bid)
    ):
        w = float(ask) - float(bid)
        if sb == "buy":
            return float(bid) + f * w
        return float(ask) - f * w
    m = float(mid_price)
    if m <= 0:
        return 0.0
    w = abs(m) * max(0.0, float(spread_pct)) / 100.0
    if sb == "buy":
        return m + (f - 0.5) * w
    return m + (0.5 - f) * w


class OrderType(Enum):
    LIMIT = "limit"
    MARKET = "market"


@dataclass
class OrderRequest:
    symbol: str
    side: str
    quantity: float
    order_type: OrderType
    limit_price: float | None = None
    expected_price: float | None = None
    #: When set (> 0), Alpaca submits a market order by ``notional`` (USD); ``quantity`` is ignored at the API.
    notional: float | None = None


@dataclass
class FillReport:
    symbol: str
    side: str
    quantity: float
    fill_price: float
    expected_price: float
    slippage_bps: float
    timestamp: datetime


@dataclass
class ExecutionState:
    fill_history: list[FillReport] = field(default_factory=list)
    strategy_slippage_bps_avg: float = 0.0
    strategy_blocked: bool = False


class ExecutionManager:
    def __init__(self, config: dict[str, Any]):
        self.config = config
        ex = config.get("execution", {})
        dyn_cfg = config.get("dynamic_universe") or {}
        try:
            self.dynamic_execution_max_spread_pct = float(
                dyn_cfg.get("execution_max_spread_pct", 8.0)
            )
        except (TypeError, ValueError):
            self.dynamic_execution_max_spread_pct = 8.0
        self.dynamic_execution_max_spread_pct = max(
            0.0, min(100.0, self.dynamic_execution_max_spread_pct)
        )
        self.dynamic_universe_symbols: set[str] = {
            str(s).strip().upper()
            for s in (dyn_cfg.get("symbols") or dyn_cfg.get("active_symbols") or [])
            if s is not None and str(s).strip()
        }
        # Equity market **buys**: true → USD notional (fractional-friendly); false → whole-share qty only.
        if "allow_fractional" in ex and ex.get("allow_fractional") is not None:
            self.allow_fractional = bool(ex.get("allow_fractional"))
        elif "allow_fractional_shares" in ex and ex.get("allow_fractional_shares") is not None:
            self.allow_fractional = bool(ex.get("allow_fractional_shares"))
        else:
            self.allow_fractional = True
        self.prefer_limit_orders = bool(ex.get("prefer_limit_orders", True))
        self.limit_order_offset_ticks = int(ex.get("limit_order_offset_ticks", 1))
        self.limit_price_mode = str(ex.get("limit_price_mode", "inside_spread")).strip().lower()
        self.inside_spread_fraction = float(ex.get("inside_spread_fraction", 0.25))
        _rd = ex.get("limit_price_round_decimals")
        try:
            self.limit_price_round_decimals = int(_rd) if _rd is not None and str(_rd).strip() != "" else 2
        except (TypeError, ValueError):
            self.limit_price_round_decimals = 2
        self.limit_price_round_decimals = max(0, min(8, self.limit_price_round_decimals))
        _ms = ex.get("max_spread_pct")
        self._tiered_spread = False
        self._cap_large = 1.0
        self._cap_mid = 1.5
        self._cap_small = 2.0
        self._large_cap_symbols: set[str] = set()
        self._mid_cap_symbols: set[str] = set()
        # True when YAML has large_caps + volatile/small_caps only (no mid_caps): unknown symbols use volatile cap.
        self._two_tier_volatile_default: bool = False

        if isinstance(_ms, dict):
            self._tiered_spread = True
            lc = _ms.get("large_caps", _ms.get("large_cap"))
            mc = _ms.get("mid_caps", _ms.get("mid_cap"))
            sc = _ms.get("volatile", _ms.get("small_caps", _ms.get("small_cap")))
            self._cap_large = float(lc if lc is not None and str(lc).strip() != "" else 1.0)
            self._cap_small = float(sc if sc is not None and str(sc).strip() != "" else 2.0)
            explicit_mid = mc is not None and str(mc).strip() != ""
            if explicit_mid:
                self._cap_mid = float(mc)
            else:
                self._cap_mid = self._cap_small
            self._two_tier_volatile_default = bool(
                lc is not None
                and str(lc).strip() != ""
                and sc is not None
                and str(sc).strip() != ""
                and not explicit_mid
            )
            self._large_cap_symbols = {
                str(x).strip().upper() for x in (ex.get("large_cap_symbols") or []) if x is not None and str(x).strip()
            }
            self._mid_cap_symbols = {
                str(x).strip().upper() for x in (ex.get("mid_cap_symbols") or []) if x is not None and str(x).strip()
            }
            # Widest cap for callers that only need an upper bound (e.g. option-quote clamp).
            self.max_spread_pct_to_trade = max(self._cap_large, self._cap_mid, self._cap_small)
        elif _ms is not None and str(_ms).strip() != "":
            v = float(_ms)
            self.max_spread_pct_to_trade = v
            self._cap_large = self._cap_mid = self._cap_small = v
        else:
            # Default 1.0% — 0.10% is too strict for IEX / many names
            v = float(ex.get("max_spread_pct_to_trade", 1.0))
            self.max_spread_pct_to_trade = v
            self._cap_large = self._cap_mid = self._cap_small = v

        _skip_raw = ex.get("skip_if_spread_above_pct")
        self._skip_if_spread_above_pct: float | None = None
        if _skip_raw is not None and str(_skip_raw).strip() != "":
            self._skip_if_spread_above_pct = float(_skip_raw)

        _mkt_spread = ex.get("market_order_if_spread_below_pct")
        self.market_order_if_spread_below_pct: float | None = None
        if _mkt_spread is not None and str(_mkt_spread).strip() != "":
            try:
                v = float(_mkt_spread)
                if v >= 0 and v == v:
                    self.market_order_if_spread_below_pct = v
            except (TypeError, ValueError):
                self.market_order_if_spread_below_pct = None

        self.partial_fill_timeout_seconds = int(ex.get("partial_fill_timeout_seconds", 30))
        self.cancel_replace_on_partial = bool(ex.get("cancel_replace_on_partial", True))
        self.max_slippage_bps = float(ex.get("max_slippage_bps", 10))
        self.block_strategy_if_slippage_bps_avg_exceeds = float(
            ex.get("block_strategy_if_slippage_bps_avg_exceeds", 25)
        )
        _mtd = ex.get("min_trade_dollars")
        try:
            self.min_trade_dollars = max(0.0, float(_mtd)) if _mtd is not None and str(_mtd).strip() != "" else 0.0
        except (TypeError, ValueError):
            self.min_trade_dollars = 0.0
        _mon = ex.get("min_order_notional")
        try:
            self.min_order_notional = max(0.0, float(_mon)) if _mon is not None and str(_mon).strip() != "" else 0.0
        except (TypeError, ValueError):
            self.min_order_notional = 0.0
        _mtv = ex.get("min_trade_value")
        try:
            self.min_trade_value = max(0.0, float(_mtv)) if _mtv is not None and str(_mtv).strip() != "" else 0.0
        except (TypeError, ValueError):
            self.min_trade_value = 0.0
        _msp = ex.get("min_sell_pct_of_position")
        try:
            self.min_sell_pct_of_position = (
                max(0.0, float(_msp)) if _msp is not None and str(_msp).strip() != "" else 0.0
            )
        except (TypeError, ValueError):
            self.min_sell_pct_of_position = 0.0

        _strat_ex = (config.get("strategy") or {}).get("exits") or {}
        _ptt_raw = _strat_ex.get("partial_trim_trigger_pct")
        try:
            self._partial_trim_trigger_pct = (
                float(_ptt_raw) if _ptt_raw is not None and str(_ptt_raw).strip() != "" else None
            )
        except (TypeError, ValueError):
            self._partial_trim_trigger_pct = None
        if self._partial_trim_trigger_pct is not None and self._partial_trim_trigger_pct <= 0:
            self._partial_trim_trigger_pct = None
        _pl_frac = _strat_ex.get("trim_fraction", _strat_ex.get("partial_exit_ratio", 0.3))
        try:
            self._partial_pl_sell_fraction = max(0.0, min(1.0, float(_pl_frac)))
        except (TypeError, ValueError):
            self._partial_pl_sell_fraction = 0.3
        _ptg_raw = _strat_ex.get(
            "disable_partial_trim_below_gross_pct",
            config.get("disable_partial_trim_below_gross_pct"),
        )
        if _ptg_raw is None:
            _ptg_raw = ex.get("disable_partial_trim_below_gross_pct")
        self._disable_partial_trim_below_gross_frac = _parse_exposure_fraction(_ptg_raw)

        self.block_exits_if_no_reentry_capacity = bool(ex.get("block_exits_if_no_reentry_capacity", False))
        self.retry_on_spread_fail = bool(ex.get("retry_on_spread_fail", False))
        self.last_order_build_reject_reason: str | None = None
        try:
            self.spread_entry_retry_attempts = max(0, int(ex.get("retry_attempts", 0)))
        except (TypeError, ValueError):
            self.spread_entry_retry_attempts = 0

        (
            self._lq_relief_enabled,
            self._lq_relief_symbols,
            self._lq_relief_hard_max_spread,
            _,
        ) = liquid_spread_relief_parse(config)

    def min_buying_power_for_equity_entry_probe(self, mid_price: float) -> float:
        """
        USD floor for pre-entry *cash vs one attempt* checks in the live loop (not final size).

        Whole-share mode uses the **last** price as the minimum (one share). Fractional mode uses
        ``max(min_order_notional, min_trade_dollars)`` when either is set, else **$1** so low-priced
        names are not blocked by a 1-share minimum.
        """
        p = max(0.0, float(mid_price))
        if not self.allow_fractional:
            return p if p > 0.0 else 0.0
        floors = [v for v in (self.min_order_notional, self.min_trade_dollars) if v > 0.0]
        if floors:
            return float(max(floors))
        return 1.0

    def _equity_buy_notional_below_min(self, symbol: str, side: str, notional_usd: float) -> bool:
        """True when ``min_trade_dollars`` blocks this equity buy notional (OCC options exempt)."""
        if self.min_trade_dollars <= 0.0:
            return False
        if str(side).strip().lower() != "buy":
            return False
        if is_option_symbol(str(symbol).strip().upper()):
            return False
        return float(notional_usd) + 1e-6 < float(self.min_trade_dollars)

    def _entry_min_notional_clip(self, symbol: str, side: str, quantity: int) -> float:
        """Entry-only fractional buy notional floor; direct ``build_order`` remains strict."""
        if not self.allow_fractional:
            return 0.0
        if str(side).strip().lower() != "buy":
            return 0.0
        if int(quantity) < 1:
            return 0.0
        if is_option_symbol(str(symbol).strip().upper()):
            return 0.0
        return max(float(self.min_trade_dollars), float(self.min_order_notional), 0.0)

    def _equity_sell_quantity_floor(
        self,
        qty_sell: float,
        position_qty: float,
        mid: float,
        symbol: str,
    ) -> float:
        """Raise partial sells to floors; include decimals on final liquidation."""
        sym_u = str(symbol).strip().upper()
        if is_option_symbol(sym_u):
            return qty_sell
        pos_qty = max(0.0, float(position_qty))
        sell_qty = max(0.0, float(qty_sell))
        if pos_qty <= 0.0 or sell_qty <= 0.0:
            return sell_qty
        remaining = pos_qty - sell_qty
        if sell_qty >= pos_qty or 0.0 < remaining < 1.0:
            return pos_qty
        if self.min_trade_value <= 0.0 and self.min_sell_pct_of_position <= 0.0:
            return float(int(sell_qty))
        if pos_qty < 1.0:
            return pos_qty
        if sell_qty < 1.0:
            return sell_qty
        floor_s = 1
        if self.min_sell_pct_of_position > 0.0:
            floor_s = max(
                floor_s,
                int(math.ceil(float(self.min_sell_pct_of_position) * float(pos_qty))),
            )
        if self.min_trade_value > 0.0 and mid > 0.0:
            floor_s = max(floor_s, int(math.ceil(float(self.min_trade_value) / float(mid))))
        target = max(int(sell_qty), floor_s)
        return float(min(target, int(pos_qty)))

    def set_dynamic_universe_symbols(self, symbols: Iterable[str] | None) -> None:
        self.dynamic_universe_symbols = {
            str(s).strip().upper()
            for s in (symbols or [])
            if s is not None and str(s).strip()
        }

    def is_dynamic_universe_symbol(self, symbol: str | None) -> bool:
        return str(symbol or "").strip().upper() in self.dynamic_universe_symbols

    def max_spread_pct_for_symbol(self, symbol: str | None, *, cap_relax_factor: float = 1.0) -> float:
        """Spread %% cap for ``can_trade_spread`` / ``build_order`` for this instrument.

        When ``execution.skip_if_spread_above_pct`` is set (e.g. ``0.5``), skip if
        ``spread_pct`` exceeds that value by using ``min(configured_cap, skip)``.
        """
        if self.is_dynamic_universe_symbol(symbol):
            cap = float(self.dynamic_execution_max_spread_pct)
        elif not self._tiered_spread:
            cap = float(self.max_spread_pct_to_trade)
        else:
            u = str(symbol or "").strip().upper()
            has_lists = bool(self._large_cap_symbols or self._mid_cap_symbols)
            if not has_lists:
                cap = self._cap_small if self._two_tier_volatile_default else self._cap_large
            elif u in self._large_cap_symbols:
                cap = self._cap_large
            elif u in self._mid_cap_symbols:
                cap = self._cap_mid
            else:
                cap = self._cap_small
        if self._skip_if_spread_above_pct is not None:
            cap = min(cap, float(self._skip_if_spread_above_pct))
        try:
            _f = max(1.0, float(cap_relax_factor or 1.0))
        except (TypeError, ValueError):
            _f = 1.0
        if _f > 1.0:
            cap = min(100.0, float(cap) * _f)
        return cap

    def can_trade_spread(
        self,
        spread_pct: float,
        symbol: str | None = None,
        *,
        ignore_spread_gate: bool = False,
        cap_relax_factor: float = 1.0,
    ) -> tuple[bool, str]:
        if ignore_spread_gate:
            return True, "ok"
        cap = self.max_spread_pct_for_symbol(symbol, cap_relax_factor=cap_relax_factor)
        if spread_pct > cap:
            su = str(symbol or "").strip().upper()
            if (
                self._lq_relief_enabled
                and su
                and su in self._lq_relief_symbols
                and (
                    self._lq_relief_hard_max_spread is None
                    or float(spread_pct) <= float(self._lq_relief_hard_max_spread) + 1e-9
                )
            ):
                return True, "ok"
            return False, f"spread {spread_pct:.4f}% > max {cap}%"
        return True, "ok"

    def build_order_for_entry(
        self,
        symbol: str,
        side: str,
        quantity: float | int,
        mid_price: float,
        spread_pct: float,
        tick_size: float = 0.01,
        *,
        ignore_spread_gate: bool = False,
        bid: float | None = None,
        ask: float | None = None,
        notional: float | None = None,
        position_qty: float | int | None = None,
        cap_relax_factor: float = 1.0,
        bypass_min_trade_dollars: bool = False,
    ) -> OrderRequest | None:
        """
        Like :meth:`build_order`, but if the first call returns ``None`` and
        ``execution.retry_on_spread_fail`` is true, issues up to ``execution.retry_attempts`` more
        attempts with ``ignore_spread_gate=True`` (spread gate only; other rejection reasons are unchanged).
        """
        req = self.build_order(
            symbol,
            side,
            quantity,
            mid_price,
            spread_pct,
            tick_size,
            ignore_spread_gate=ignore_spread_gate,
            bid=bid,
            ask=ask,
            notional=notional,
            position_qty=position_qty,
            cap_relax_factor=cap_relax_factor,
            bypass_min_trade_dollars=bypass_min_trade_dollars,
        )
        if req is not None:
            return req
        min_entry_notional = self._entry_min_notional_clip(symbol, side, quantity)
        if min_entry_notional > 0.0 and "below min_trade_dollars" in (
            self.last_order_build_reject_reason or ""
        ):
            req = self.build_order(
                symbol,
                side,
                quantity,
                mid_price,
                spread_pct,
                tick_size,
                ignore_spread_gate=ignore_spread_gate,
                bid=bid,
                ask=ask,
                notional=min_entry_notional,
                position_qty=position_qty,
                cap_relax_factor=cap_relax_factor,
                bypass_min_trade_dollars=bypass_min_trade_dollars,
            )
            if req is not None:
                return req
        if (
            not self.retry_on_spread_fail
            or self.spread_entry_retry_attempts < 1
            or not (self.last_order_build_reject_reason or "").startswith("spread ")
        ):
            return None
        for _ in range(int(self.spread_entry_retry_attempts)):
            req = self.build_order(
                symbol,
                side,
                quantity,
                mid_price,
                spread_pct,
                tick_size,
                ignore_spread_gate=True,
                bid=bid,
                ask=ask,
                notional=notional,
                position_qty=position_qty,
                cap_relax_factor=cap_relax_factor,
                bypass_min_trade_dollars=bypass_min_trade_dollars,
            )
            if req is not None:
                return req
        return None

    def partial_exit_sell_quantity(
        self,
        *,
        tracker_qty: int,
        broker_position: Mapping[str, Any],
        current_gross_pct: float | None = None,
    ) -> int:
        """
        Whole-share sell count from broker unrealized P/L%% via :func:`~src.exits.check_partial_exit`.

        Merges ``broker_position`` (``unrealized_plpc`` / ``market_value`` / etc.) with
        ``tracker_qty`` as ``qty``. Returns ``0`` when no trim, else
        ``max(1, min(round(qty * fraction), qty))``.
        """
        from .exits import check_partial_exit

        if tracker_qty <= 0:
            return 0
        if self._disable_partial_trim_below_gross_frac is not None and current_gross_pct is not None:
            try:
                gross_frac = float(current_gross_pct) / 100.0
            except (TypeError, ValueError):
                gross_frac = float("nan")
            if math.isfinite(gross_frac) and gross_frac < float(self._disable_partial_trim_below_gross_frac):
                return 0
        row = dict(broker_position)
        row["qty"] = int(tracker_qty)
        frac = float(
            check_partial_exit(
                row,
                partial_trim_trigger_pct=self._partial_trim_trigger_pct,
                sell_fraction=self._partial_pl_sell_fraction,
            )
        )
        if frac <= 0.0:
            return 0
        raw = int(round(float(tracker_qty) * frac))
        return max(1, min(raw, int(tracker_qty)))

    def build_order(
        self,
        symbol: str,
        side: str,
        quantity: float | int,
        mid_price: float,
        spread_pct: float,
        tick_size: float = 0.01,
        *,
        ignore_spread_gate: bool = False,
        bid: float | None = None,
        ask: float | None = None,
        notional: float | None = None,
        position_qty: float | int | None = None,
        cap_relax_factor: float = 1.0,
        bypass_min_trade_dollars: bool = False,
    ) -> OrderRequest | None:
        self.last_order_build_reject_reason = None
        allowed, _ = self.can_trade_spread(
            spread_pct, symbol, ignore_spread_gate=ignore_spread_gate, cap_relax_factor=cap_relax_factor
        )
        if not allowed:
            self.last_order_build_reject_reason = (
                f"spread {float(spread_pct):.4f}% > max "
                f"{self.max_spread_pct_for_symbol(symbol, cap_relax_factor=cap_relax_factor):.4f}%"
            )
            return None

        try:
            n = float(notional) if notional is not None and str(notional).strip() != "" else 0.0
        except (TypeError, ValueError):
            n = 0.0
        if n > 0:
            side_l0 = str(side).lower()
            mid0 = float(mid_price)
            sym0 = str(symbol).strip().upper()
            if not bypass_min_trade_dollars and self._equity_buy_notional_below_min(sym0, side_l0, n):
                self.last_order_build_reject_reason = (
                    f"notional ${n:.2f} below min_trade_dollars ${self.min_trade_dollars:.2f}"
                )
                return None
            if (
                not self.allow_fractional
                and side_l0 == "buy"
                and not is_option_symbol(sym0)
                and mid0 > 0
            ):
                q0 = int(n / mid0)
                if q0 < 1:
                    self.last_order_build_reject_reason = (
                        "notional converts to zero whole shares"
                    )
                    return None
                return OrderRequest(
                    symbol=symbol,
                    side=side_l0,
                    quantity=q0,
                    order_type=OrderType.MARKET,
                    limit_price=None,
                    expected_price=mid0,
                    notional=None,
                )
            # Alpaca equities: notional is market-only (cannot combine with limit in SDK / API).
            return OrderRequest(
                symbol=symbol,
                side=str(side).lower(),
                quantity=max(0.0, float(quantity)),
                order_type=OrderType.MARKET,
                limit_price=None,
                expected_price=float(mid_price),
                notional=n,
            )

        try:
            qty_raw = float(quantity)
        except (TypeError, ValueError):
            qty_raw = 0.0
        qty_i = float(int(qty_raw))
        mid = float(mid_price)
        side_l = str(side).lower()
        if qty_raw <= 0.0:
            self.last_order_build_reject_reason = "quantity <= 0"
            return None
        if (
            side_l == "sell"
            and position_qty is not None
            and not is_option_symbol(str(symbol).strip().upper())
        ):
            qty_i = self._equity_sell_quantity_floor(qty_raw, float(position_qty), mid, str(symbol))
            if qty_i <= 0.0:
                self.last_order_build_reject_reason = "sell quantity below floor"
                return None
        if is_option_symbol(symbol) and qty_i > 0.0 and mid > 0:
            if self.limit_price_mode == "inside_spread":
                raw_lp = limit_price_inside_spread(
                    side,
                    bid=bid,
                    ask=ask,
                    mid_price=mid_price,
                    spread_pct=spread_pct,
                    inside_fraction=self.inside_spread_fraction,
                )
                limit_price = round(raw_lp, self.limit_price_round_decimals)
            else:
                offset = self.limit_order_offset_ticks * tick_size
                if side == "buy":
                    limit_price = round(mid_price - offset, self.limit_price_round_decimals)
                else:
                    limit_price = round(mid_price + offset, self.limit_price_round_decimals)
            return OrderRequest(
                symbol=symbol,
                side=side_l,
                quantity=qty_i,
                order_type=OrderType.LIMIT,
                limit_price=limit_price,
                expected_price=mid,
                notional=None,
            )

        if not is_option_symbol(symbol) and qty_i > 0.0 and mid > 0:
            thr = self.market_order_if_spread_below_pct
            sp = float(spread_pct)
            use_market = thr is None or sp < thr
            if use_market:
                # Partial equity sells stay whole-share; final liquidations may include
                # fractional broker quantity so no sub-1 share tail remains.
                if side_l == "sell":
                    return OrderRequest(
                        symbol=symbol,
                        side=side_l,
                        quantity=qty_i,
                        order_type=OrderType.MARKET,
                        limit_price=None,
                        expected_price=mid,
                        notional=None,
                    )
                if self.allow_fractional:
                    derived = round(float(qty_i) * mid, 2)
                    if derived > 0:
                        if self._equity_buy_notional_below_min(str(symbol).strip().upper(), side_l, derived):
                            self.last_order_build_reject_reason = (
                                f"derived notional ${derived:.2f} below min_trade_dollars "
                                f"${self.min_trade_dollars:.2f}"
                            )
                            return None
                        return OrderRequest(
                            symbol=symbol,
                            side=side_l,
                            quantity=qty_i,
                            order_type=OrderType.MARKET,
                            limit_price=None,
                            expected_price=mid,
                            notional=derived,
                        )
                return OrderRequest(
                    symbol=symbol,
                    side=side_l,
                    quantity=qty_i,
                    order_type=OrderType.MARKET,
                    limit_price=None,
                    expected_price=mid,
                    notional=None,
                )
            else:
                lp = round(mid, self.limit_price_round_decimals)
                return OrderRequest(
                    symbol=symbol,
                    side=side_l,
                    quantity=qty_i,
                    order_type=OrderType.LIMIT,
                    limit_price=lp,
                    expected_price=mid,
                    notional=None,
                )

        if self.prefer_limit_orders:
            if self.limit_price_mode == "inside_spread":
                raw_lp = limit_price_inside_spread(
                    side,
                    bid=bid,
                    ask=ask,
                    mid_price=mid_price,
                    spread_pct=spread_pct,
                    inside_fraction=self.inside_spread_fraction,
                )
                limit_price = round(raw_lp, self.limit_price_round_decimals)
            else:
                offset = self.limit_order_offset_ticks * tick_size
                if side == "buy":
                    limit_price = round(mid_price - offset, self.limit_price_round_decimals)
                else:
                    limit_price = round(mid_price + offset, self.limit_price_round_decimals)
            return OrderRequest(
                symbol=symbol,
                side=side,
                quantity=quantity,
                order_type=OrderType.LIMIT,
                limit_price=limit_price,
                expected_price=mid_price,
                notional=None,
            )
        return OrderRequest(
            symbol=symbol,
            side=side,
            quantity=quantity,
            order_type=OrderType.MARKET,
            expected_price=mid_price,
            notional=None,
        )

    def build_order_from_dict(
        self,
        spec: Mapping[str, Any],
        mid_price: float,
        spread_pct: float,
        tick_size: float = 0.01,
        *,
        ignore_spread_gate: bool = False,
        bid: float | None = None,
        ask: float | None = None,
        bypass_min_trade_dollars: bool = False,
    ) -> OrderRequest | None:
        """
        Build an :class:`OrderRequest` from a small dict.

        Supports ``{"symbol", "side", "notional"}`` (USD market) or ``qty`` / ``quantity`` for share count.
        """
        symbol = str(spec.get("symbol") or "").strip()
        route = spec.get("route")
        source = spec.get("source")
        strategy = spec.get("strategy") or route or source
        raw_n = spec.get("notional")
        raw_q = spec.get("qty", spec.get("quantity"))
        if not symbol:
            self.last_order_build_reject_reason = "missing symbol"
            _log_order_build_reject(
                symbol=symbol,
                reason="missing_symbol",
                route=route,
                source=source,
                notional=raw_n,
                qty=raw_q,
            )
            return None
        side = str(spec.get("side") or "buy").strip().lower()
        if raw_n is not None and str(raw_n).strip() != "":
            try:
                n = float(raw_n)
            except (TypeError, ValueError):
                self.last_order_build_reject_reason = "invalid notional"
                _log_order_build_reject(
                    symbol=symbol,
                    reason="invalid_notional",
                    route=route,
                    source=source,
                    notional=raw_n,
                    qty=raw_q,
                )
                return None
            if n > 0:
                req = self.build_order(
                    symbol,
                    side,
                    0,
                    mid_price,
                    spread_pct,
                    tick_size,
                    ignore_spread_gate=ignore_spread_gate,
                    bid=bid,
                    ask=ask,
                    notional=n,
                    bypass_min_trade_dollars=bypass_min_trade_dollars,
                )
                if req is None:
                    _log_order_build_reject(
                        symbol=symbol,
                        reason=self.last_order_build_reject_reason or "build_order_rejected",
                        route=route,
                        source=source,
                        notional=n,
                        qty=0,
                    )
                else:
                    setattr(req, "route", route)
                    setattr(req, "source", source)
                    setattr(req, "strategy", strategy)
                    setattr(req, "core_rebuild", spec.get("core_rebuild"))
                return req
        try:
            qty = int(raw_q) if raw_q is not None and str(raw_q).strip() != "" else 0
        except (TypeError, ValueError):
            qty = 0
        if qty < 1:
            self.last_order_build_reject_reason = "quantity < 1"
            _log_order_build_reject(
                symbol=symbol,
                reason="quantity_lt_1",
                route=route,
                source=source,
                notional=raw_n,
                qty=raw_q,
            )
            return None
        pos_raw = spec.get("position_qty")
        try:
            pos_q = int(pos_raw) if pos_raw is not None and str(pos_raw).strip() != "" else None
        except (TypeError, ValueError):
            pos_q = None
        req = self.build_order(
            symbol,
            side,
            qty,
            mid_price,
            spread_pct,
            tick_size,
            ignore_spread_gate=ignore_spread_gate,
            bid=bid,
            ask=ask,
            position_qty=pos_q,
        )
        if req is None:
            _log_order_build_reject(
                symbol=symbol,
                reason=self.last_order_build_reject_reason or "build_order_rejected",
                route=route,
                source=source,
                notional=raw_n,
                qty=qty,
            )
        else:
            setattr(req, "route", route)
            setattr(req, "source", source)
            setattr(req, "strategy", strategy)
            setattr(req, "core_rebuild", spec.get("core_rebuild"))
        return req

    def record_fill(
        self,
        state: ExecutionState,
        symbol: str,
        side: str,
        quantity: int,
        fill_price: float,
        expected_price: float,
    ) -> None:
        if expected_price <= 0:
            slippage_bps = 0.0
        else:
            if side == "buy":
                slippage_bps = (fill_price - expected_price) / expected_price * 10_000
            else:
                slippage_bps = (expected_price - fill_price) / expected_price * 10_000

        state.fill_history.append(
            FillReport(
                symbol=symbol,
                side=side,
                quantity=quantity,
                fill_price=fill_price,
                expected_price=expected_price,
                slippage_bps=slippage_bps,
                timestamp=datetime.utcnow(),
            )
        )

        n = len(state.fill_history)
        if n > 0:
            state.strategy_slippage_bps_avg = sum(f.slippage_bps for f in state.fill_history) / n
            if state.strategy_slippage_bps_avg > self.block_strategy_if_slippage_bps_avg_exceeds:
                state.strategy_blocked = True

    def should_block_strategy(self, state: ExecutionState) -> bool:
        return state.strategy_blocked

    def partial_fill_should_cancel_replace(self, filled_qty: int, requested_qty: int) -> bool:
        if not self.cancel_replace_on_partial:
            return False
        return 0 < filled_qty < requested_qty
