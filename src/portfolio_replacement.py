"""
Portfolio cap helpers and rotation utilities.

At ``max_positions`` with ``portfolio.enable_replacement``, live trend-long stock routing uses
:mod:`src.portfolio_score_replacement` (strength gap when ``replacement.replacement_threshold``
is in (0, 1), unless ``replacement.rotate_on_stronger_signal`` — then incoming must only exceed weakest;
else signal vs position scores and ``portfolio.swap_threshold``). This module still provides:

* :func:`weakest_replacement_hold` / :func:`tracked_signal_strength` /
  :func:`hold_health_score_normalized` for rebalance-free-capital and tests (``replacement.weakest_pick``)
* :func:`replacement_strength_ok`, :func:`replacement_rotate_target_ok` (legacy strength comparison)
* ``replacement.strategy: min_unrealized_pl_vs_signal`` — :func:`consider_replacement` /
  :func:`replacement_weakest_row_by_unrealized_pl_pct` / :func:`replacement_incoming_strong_enough_vs_weakest` /
  :func:`replacement_is_stronger_incoming_vs_weakest` / :func:`replacement_is_strong_entry_eval`
  (weakest by min unrealized P/L % vs incoming strength + ``is_stronger(sig, pos)`` when eval known), then :func:`replacement_common_post_weakest_pick`
* ``replacement.churn_guard_min_new_vs_weakest_ratio`` (default ``1.2``) — require
  ``new_strength > weakest_strength * ratio`` to rotate; use ``1.0`` to disable the margin.
* Step 3 **structural strong** (when trend/momentum/pullback are all known from
  :meth:`~src.strategy.TrendFollowingStrategy.entry_eval_components_for_log`): require
  ``trend and momentum and pullback`` **and** the churn guard above. Unknown components skip this gate.
* :func:`replacement_min_market_value_to_replace_usd`, :func:`replacement_min_notional_for_incoming_usd`,
  :func:`replacement_max_per_cycle`, :func:`replacement_strength_gap_ok`, :func:`replacement_size_ok`
* Eligible-symbol and short-circuit helpers

Deferred replacement ties may use ``portfolio.priority_symbols`` (see :func:`stronger_deferred_replacement_row`).

reads ``config["portfolio"]``: ``max_positions`` (default :data:`MAX_POSITIONS` = 10 when omitted — top-N book),
``allow_add``, ``enable_replacement``,
``replacement.*``, ``allowed_symbols_for_stock_orders``.

Legacy compatibility module: newer portfolio package code lives under
``src/portfolio/``. This root module is still the active import path for a
large part of the codebase, so keep it in place until those callers migrate.
"""
from __future__ import annotations

import logging
import random
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any

import pandas as pd

from .exposure_gates import strong_signal_cap_relief_eligible_for_symbol
from .options_premium_risk import is_option_symbol
from .portfolio_allocation import symbol_long_position_market_value_usd
from .position_scoring import (
    composite_position_score,
    pnl_score_01,
    position_dict_for_signal_score,
)
from .position_tracker import bars_held, minutes_held, tracked_row_has_open_long

log = logging.getLogger(__name__)

# ``portfolio.replacement.weakest_pick``: persisted entry strength vs live health composite.
WEAKEST_PICK_ENTRY_SIGNAL_STRENGTH = "entry_signal_strength"
WEAKEST_PICK_PNL_MOMENTUM_TREND = "pnl_momentum_trend"
# **Weakest (simple):** score = ``momentum_score + pnl_leg + trend_strength`` (``[0, 3]``); *lowest* = weakest
# (``pnl_leg`` = :func:`~src.position_scoring.pnl_score_01`, often written *pnl_pct* in specs). Same as
# :data:`WEAKEST_PICK_COMPOSITE_POSITION_SCORE` / :func:`weakest_position_score_momentum_pnl_trend`.
# **pnl_momentum_trend** (health) uses the *mean* ``/3`` on ``[0,1]`` — not the same as this sum.
WEAKEST_PICK_COMPOSITE_POSITION_SCORE = "composite_position_score"

def replacement_entry_fail_reason_invites_cap_rotation(reason: str | None) -> bool:
    """
    Step 1 — when entry fails due to caps, try rotation: true if ``\"cap\"`` appears in *reason*
    (case-insensitive), e.g. portfolio / symbol / sector cap sizing messages or ``exposure_gate: ... cap``.
    """
    if reason is None or not str(reason).strip():
        return False
    return "cap" in str(reason).lower()


def portfolio_budget_cap_sizing_reject(reason: str | None) -> bool:
    """
    True when :meth:`~src.position_sizing.PositionSizer.size_position` exhausted gross/net/theme
    headroom (reject ``portfolio caps leave no room``). Used to trigger partial book trims before
    full weakest-line rotation.
    """
    if reason is None or not str(reason).strip():
        return False
    return "portfolio caps leave no room" in str(reason).lower()


def parse_cap_pressure_trim_cfg(portfolio_cfg: Mapping[str, Any] | None) -> tuple[bool, float, int]:
    """
    Read ``portfolio.cap_pressure_trim``: ``enabled``, ``trim_frac`` (clamped to [0.10, 0.20]),
    ``max_symbols_per_cycle``.
    """
    if not isinstance(portfolio_cfg, dict):
        return False, 0.15, 24
    raw = portfolio_cfg.get("cap_pressure_trim")
    if not isinstance(raw, dict):
        return False, 0.15, 24
    en = raw.get("enabled", True)
    if isinstance(en, str):
        en = str(en).strip().lower() in ("1", "true", "yes", "on")
    if not bool(en):
        return False, 0.15, 24
    try:
        frac = float(raw.get("trim_frac", 0.15))
    except (TypeError, ValueError):
        frac = 0.15
    frac = max(0.10, min(0.20, frac))
    try:
        max_syms = int(raw.get("max_symbols_per_cycle", 24))
    except (TypeError, ValueError):
        max_syms = 24
    max_syms = max(1, min(max_syms, 500))
    return True, frac, max_syms

# ``portfolio.replacement.strategy`` — ``min_unrealized_pl_vs_signal`` uses :func:`consider_replacement`.
REPLACEMENT_STRATEGY_DEFAULT = "default"
REPLACEMENT_STRATEGY_MIN_UNREALIZED_PL_VS_SIGNAL = "min_unrealized_pl_vs_signal"

# Step 3 churn guard: require ``new_strength > weakest_strength * ratio`` (see :func:`consider_replacement`).
DEFAULT_CHURN_GUARD_MIN_NEW_VS_WEAKEST_RATIO = 1.2


def replacement_churn_guard_min_new_vs_weakest_ratio(rep_sub: Mapping[str, Any] | None) -> float:
    """
    Minimum ``incoming / weakest`` strength ratio for ``min_unrealized_pl_vs_signal`` replacement.

    ``1.0`` = no margin (strict ``new > weakest``). Values ``> 1.0`` require incoming to clear
    ``weakest * ratio``. When the key is omitted, returns :data:`DEFAULT_CHURN_GUARD_MIN_NEW_VS_WEAKEST_RATIO`.
    """
    rep = rep_sub if isinstance(rep_sub, dict) else {}
    raw = rep.get("churn_guard_min_new_vs_weakest_ratio")
    if raw is None or str(raw).strip() == "":
        return float(DEFAULT_CHURN_GUARD_MIN_NEW_VS_WEAKEST_RATIO)
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return float(DEFAULT_CHURN_GUARD_MIN_NEW_VS_WEAKEST_RATIO)
    if v <= 1.0:
        return 1.0
    return v


def parse_replacement_strategy(rep_sub: Mapping[str, Any] | None) -> str:
    """Read ``replacement.strategy`` (alias ``replacement_strategy``)."""
    rep = rep_sub if isinstance(rep_sub, dict) else {}
    raw = rep.get("strategy") or rep.get("replacement_strategy")
    if raw is None or str(raw).strip() == "":
        return REPLACEMENT_STRATEGY_DEFAULT
    s = str(raw).strip().lower().replace("-", "_")
    if s in (
        "min_unrealized_pl_vs_signal",
        "step2",
        "min_pl",
        "min_unrealized_pl",
        "pl_pct_min",
    ):
        return REPLACEMENT_STRATEGY_MIN_UNREALIZED_PL_VS_SIGNAL
    return REPLACEMENT_STRATEGY_DEFAULT


def unrealized_pl_frac_for_sort(p: Mapping[str, Any]) -> float:
    """Fractional unrealized P/L (``unrealized_pl_pct``; Alpaca ``unrealized_plpc``), for ``min(..., key=)``."""
    for k in ("unrealized_plpc", "unrealized_intraday_plpc"):
        raw = p.get(k)
        if raw is not None and str(raw).strip() != "":
            try:
                return float(raw)
            except (TypeError, ValueError):
                return 0.0
    ur = p.get("unrealized_pl")
    mv = p.get("market_value")
    try:
        if ur is not None and mv not in (None, 0, "", "0") and float(mv) != 0:
            return float(ur) / float(mv)
    except (TypeError, ValueError, ZeroDivisionError):
        pass
    return 0.0


def position_strength_hold(p: Mapping[str, Any]) -> float:
    """
    Normalized hold quality on ``[0, 1]`` from broker P/L — comparable to :func:`new_signal_strength_replacement`.
    """
    sym = str(p.get("symbol") or "").strip().upper()
    if not sym:
        return 0.5
    pdct = position_dict_for_signal_score(sym, [p])
    return float(pnl_score_01(pdct))


def new_signal_strength_replacement(new_signal: Any, strength_jitter_max: float) -> float:
    """Jittered entry ``signal_strength`` on ``[0, 1]`` (see :func:`effective_signal_strength`)."""
    base = float(getattr(getattr(new_signal, "entry_signal", None), "strength", None) or 1.0)
    return float(effective_signal_strength(base, strength_jitter_max))


def replacement_is_strong_entry_eval(
    trend: bool | None,
    momentum: bool | None,
    pullback: bool | None,
) -> bool | None:
    """
    Step 3 structural **strong** (same booleans as ENTRY_EVAL / strategy context):

    ``return trend and momentum and pullback`` — in Python, require each to be exactly ``True``.

    Returns ``None`` if any argument is ``None`` (caller skips this gate and uses numeric strength only).
    Returns ``False`` if any is ``False``. Returns ``True`` only when all three are ``True``.

    Note: *momentum* here is the third value from :meth:`~src.strategy.TrendFollowingStrategy.entry_eval_components_for_log`
    (``True`` when ``entry_mode == \"momentum\"``), not a separate momentum-quality score. In pullback
    ``entry_mode`` that flag is ``False``, so this gate never passes — only disable by passing all-``None``
    components or omitting them at the call site.
    """
    if trend is None or momentum is None or pullback is None:
        return None
    return bool(trend and momentum and pullback)


def replacement_is_stronger_incoming_vs_weakest(
    trend: bool | None,
    momentum: bool | None,
    pullback: bool | None,
    weakest_row: Mapping[str, Any],
) -> bool | None:
    """
    ``is_stronger(sig, pos)`` for rotation: incoming ENTRY_EVAL triple on the signal **and** the
    candidate weakest hold is a weak performer on unrealized P/L fraction:

    ``sig.momentum and sig.trend and sig.pullback and pos.unrealized_pl_pct < 1.0``

    ``unrealized_pl_pct`` is :func:`unrealized_pl_frac_for_sort` (Alpaca-style fractional P/L, e.g.
    ``0.03`` for +3%, ``1.2`` for +120%). The ``< 1.0`` cutoff excludes holds already up 100%+ on
    that scale (rotation would not sell them under this rule).

    Returns ``None`` if any of *trend* / *momentum* / *pullback* is ``None`` (caller skips this combined gate).
    """
    if trend is None or momentum is None or pullback is None:
        return None
    if not (trend and momentum and pullback):
        return False
    pl = unrealized_pl_frac_for_sort(weakest_row)
    return bool(pl < 1.0)


def replacement_incoming_entry_eval_triple(
    *,
    engine: Any | None,
    incoming_sym_upper: str,
    df: Any,
    spread_pct: float | None,
    atr_pct: float | None,
    regime_score: int | None,
) -> tuple[bool | None, bool | None, bool | None]:
    """
    Fetch ``(trend, momentum, pullback)`` for the incoming symbol using
    :meth:`~src.strategy.TrendFollowingStrategy.entry_eval_components_for_log`
    (reorders the strategy tuple ``(trend, pullback, momentum, vol)`` for :func:`replacement_is_stronger_incoming_vs_weakest`).
    Returns ``(None, None, None)`` when *engine* / bars are unusable.
    """
    strat = getattr(engine, "strategy", None) if engine is not None else None
    if strat is None:
        return (None, None, None)
    sym = str(incoming_sym_upper).strip().upper()
    if not sym:
        return (None, None, None)
    try:
        if df is None or not isinstance(df, pd.DataFrame) or df.empty:
            return (None, None, None)
        trend_b, pullback_b, momentum_b, _vol = strat.entry_eval_components_for_log(
            sym, df, spread_pct, atr_pct, regime_score=regime_score
        )
        return (trend_b, momentum_b, pullback_b)
    except Exception:
        log.debug("replacement entry_eval triple failed for %s", sym, exc_info=True)
        return (None, None, None)


def replacement_weakest_row_by_unrealized_pl_pct(
    rows: Sequence[Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    """
    Step 2 — ``weakest = min(positions, key=unrealized_pl_pct)``: pick the row with the lowest
    fractional unrealized P/L (see :func:`unrealized_pl_frac_for_sort`), tie-broken by symbol.
    """
    rlist = [p for p in rows if isinstance(p, dict)]
    if not rlist:
        return None
    return min(rlist, key=lambda p: (unrealized_pl_frac_for_sort(p), str(p.get("symbol") or "").upper()))


def replacement_incoming_strong_enough_vs_weakest(
    new_signal: Any,
    weakest_row: Mapping[str, Any],
    *,
    strength_jitter_max: float,
    churn_guard_min_new_vs_weakest_ratio: float,
    entry_eval_trend: bool | None = None,
    entry_eval_momentum: bool | None = None,
    entry_eval_pullback: bool | None = None,
) -> bool:
    """
    **Strong** for rotation combines:

    * **Step 3:** when *entry_eval_* are all non-``None``, require :func:`replacement_is_stronger_incoming_vs_weakest`
      (incoming triple **and** ``unrealized_pl_frac_for_sort(weakest) < 1.0``). When any entry_eval is ``None``,
      this sub-gate is skipped.
    * **Churn guard:** :func:`position_strength_hold` on the weakest row vs
      :func:`new_signal_strength_replacement` — ``new > weakest * ratio`` (``ratio=1.0`` ⇒ strict ``new > weakest``).
    """
    stronger = replacement_is_stronger_incoming_vs_weakest(
        entry_eval_trend,
        entry_eval_momentum,
        entry_eval_pullback,
        weakest_row,
    )
    if stronger is False:
        return False
    w_strength = position_strength_hold(weakest_row)
    n_strength = new_signal_strength_replacement(new_signal, strength_jitter_max)
    ratio = max(float(churn_guard_min_new_vs_weakest_ratio), 1.0)
    floor = float(w_strength) * ratio
    eps = 1e-9
    numeric_ok = n_strength > floor + eps
    if stronger is None:
        return numeric_ok
    return numeric_ok and stronger


def consider_replacement(
    new_signal: Any,
    *,
    positions: Sequence[Mapping[str, Any]],
    eligible_symbols: Sequence[str],
    incoming_sym_upper: str,
    strength_jitter_max: float = 0.0,
    churn_guard_min_new_vs_weakest_ratio: float = DEFAULT_CHURN_GUARD_MIN_NEW_VS_WEAKEST_RATIO,
    entry_eval_trend: bool | None = None,
    entry_eval_momentum: bool | None = None,
    entry_eval_pullback: bool | None = None,
) -> str | None:
    """
    Step 2 rotation (sell weakest, then caller buys *new_signal*):

    .. code-block:: text

        weakest = min(eligible_longs, key=unrealized_pl_pct)   # :func:`replacement_weakest_row_by_unrealized_pl_pct`
        if is_strong(new_signal):                              # :func:`replacement_incoming_strong_enough_vs_weakest`
            return weakest.symbol   # caller sells, then buys new_signal

    ``unrealized_pl_pct`` is :func:`unrealized_pl_frac_for_sort` (``unrealized_plpc`` / ``unrealized_pl`` / MV).

    **Step 3 — strong:** when optional ``entry_eval_*`` are all set (non-``None``), require
    :func:`replacement_is_stronger_incoming_vs_weakest` (triple **and** weakest ``unrealized_pl_pct < 1.0``),
    **and** the churn guard ``new_strength > weakest_strength * churn_guard_min_new_vs_weakest_ratio`` (default ``1.2``).
    Omit the ``entry_eval_*`` arguments (default ``None``) to use the numeric churn gate only.

    Returns the symbol to sell, or ``None`` if there is no eligible candidate or the gate fails.
    """
    su = str(incoming_sym_upper).strip().upper()
    elig = {str(x).strip().upper() for x in eligible_symbols if x and str(x).strip()}
    rows: list[dict[str, Any]] = []
    for p in positions or []:
        if not isinstance(p, dict):
            continue
        sym = str(p.get("symbol") or "").strip().upper()
        if not sym or sym == su:
            continue
        if sym not in elig:
            continue
        if str(p.get("side", "long") or "long").strip().lower() == "short":
            continue
        try:
            qty = int(float(p.get("qty") or 0))
        except (TypeError, ValueError):
            qty = 0
        if qty <= 0:
            continue
        if is_option_symbol(sym):
            continue
        rows.append(p)
    weakest_row = replacement_weakest_row_by_unrealized_pl_pct(rows)
    if weakest_row is None:
        return None
    wsym = str(weakest_row.get("symbol") or "").strip().upper()
    if not wsym or wsym == su:
        return None
    if replacement_incoming_strong_enough_vs_weakest(
        new_signal,
        weakest_row,
        strength_jitter_max=strength_jitter_max,
        churn_guard_min_new_vs_weakest_ratio=churn_guard_min_new_vs_weakest_ratio,
        entry_eval_trend=entry_eval_trend,
        entry_eval_momentum=entry_eval_momentum,
        entry_eval_pullback=entry_eval_pullback,
    ):
        return wsym
    return None


def replacement_common_post_weakest_pick(
    *,
    weakest_sym: str,
    incoming_sym_upper: str,
    tracked: Mapping[str, Any],
    positions: Sequence[Mapping[str, Any]],
    dt: Any,
    rep_sub: Mapping[str, Any] | None,
    incoming_notional_usd: float,
    max_position_age_bars: int | None,
) -> tuple[str | None, str | None]:
    """
    Size / age / min-hold gates after a concrete weakest symbol has been chosen (any strategy).
    """
    su = str(incoming_sym_upper).strip().upper()
    rep = rep_sub if isinstance(rep_sub, dict) else {}
    if weakest_sym == su:
        return None, None
    w_mv = symbol_long_position_market_value_usd(list(positions), weakest_sym)
    _ok_sz, _reason_sz = replacement_size_ok(
        weakest_market_value_usd=w_mv,
        incoming_notional_usd=float(incoming_notional_usd or 0.0),
        rep_cfg=rep,
    )
    if not _ok_sz:
        return None, _reason_sz

    w_row = (tracked or {}).get(weakest_sym) if isinstance(tracked, dict) else None
    w_entry_iso = (w_row or {}).get("entry_time") if isinstance(w_row, dict) else None
    w_age: int | None = None
    if w_entry_iso:
        try:
            w_age = int(bars_held(str(w_entry_iso), dt))
        except (TypeError, ValueError):
            w_age = None

    if max_position_age_bars is not None and max_position_age_bars > 0:
        if w_age is None or w_age < int(max_position_age_bars):
            return None, (
                "portfolio replacement: weakest %s bars_held=%s < max_position_age_bars=%d"
                % (weakest_sym, w_age, int(max_position_age_bars))
            )

    _mh_raw = rep.get("min_hold_minutes")
    _mh_override: float | None
    if _mh_raw is None or str(_mh_raw).strip() == "":
        _mh_override = None
    else:
        try:
            _mh_override = float(_mh_raw)
        except (TypeError, ValueError):
            _mh_override = None

    _ok_mh, _mh_reason = replacement_weakest_min_hold_ok(
        weakest_entry_time_iso=str(w_entry_iso) if w_entry_iso else None,
        now=dt,
        min_hold_minutes=_mh_override,
    )
    if not _ok_mh:
        return None, _mh_reason or "portfolio replacement: min hold not met"

    return weakest_sym, None


def strategy_bar_params_for_position_score(engine: Any | None) -> tuple[int, int, int]:
    """Bar windows for :func:`position_scoring.composite_position_score` / health scoring."""
    return _strategy_health_params(getattr(engine, "strategy", None) if engine is not None else None)

# Broker position rows at or above this cap short-circuit the live trend-long symbol scan when
# add-ons, replacement, and top-N batch mode are off (avoids per-symbol skips and downstream API work).
# Also the default ``portfolio.max_positions`` (top-N book) when YAML omits the key.
MAX_POSITIONS = 10


def max_portfolio_positions_from_config(portfolio_cfg: Mapping[str, Any] | None) -> int:
    """
    Distinct long equity names cap — **top-N portfolio** (``portfolio.max_positions``).

    If the key is missing, empty, non-numeric, or ``<= 0``, falls back to ``portfolio.allocator.max_positions``
    when set, else :data:`MAX_POSITIONS` (10). Values ``>= 1_000_000_000`` preserve the legacy
    “unbounded” sentinel used with replacement / cap checks.
    """
    if not isinstance(portfolio_cfg, dict):
        return int(MAX_POSITIONS)
    raw = portfolio_cfg.get("max_positions")
    if raw is None or str(raw).strip() == "":
        alloc = portfolio_cfg.get("allocator")
        if isinstance(alloc, dict):
            raw_a = alloc.get("max_positions")
            if raw_a is not None and str(raw_a).strip() != "":
                try:
                    v_a = int(float(raw_a))
                    if v_a > 0:
                        return v_a
                except (TypeError, ValueError):
                    pass
        return int(MAX_POSITIONS)
    try:
        v = int(float(raw))
    except (TypeError, ValueError):
        return int(MAX_POSITIONS)
    if v <= 0:
        return int(MAX_POSITIONS)
    return v


# Weakest position must be held at least this long (wall-clock minutes from tracker entry_time)
# before it may be sold for portfolio replacement (override via ``portfolio.replacement.min_hold_minutes``).
# Default 90 aligns with the 60–120m churn floor when config is absent.
MIN_HOLD_TIME_MINUTES = 90.0

# Live trend-long scan: after deferring replacement candidates, execute at most this many
# sell-weakest + buy-new flows per user pass (each revalidated against fresh positions).
# Override with ``portfolio.replacement.max_replacements_per_entry_cycle`` (<=0 = use this default).
MAX_REPLACEMENTS_PER_LOOP = 2


def max_replacements_per_entry_cycle(portfolio_cfg: Mapping[str, Any] | None) -> int:
    """Cap replacement sells per entry scan; ``<= 0`` falls back to :data:`MAX_REPLACEMENTS_PER_LOOP`."""
    rep = (portfolio_cfg or {}).get("replacement") if isinstance(portfolio_cfg, dict) else None
    rep = rep if isinstance(rep, dict) else {}
    raw = rep.get("max_replacements_per_entry_cycle")
    if raw is None or str(raw).strip() == "":
        return int(MAX_REPLACEMENTS_PER_LOOP)
    try:
        v = int(float(raw))
    except (TypeError, ValueError):
        return int(MAX_REPLACEMENTS_PER_LOOP)
    return int(MAX_REPLACEMENTS_PER_LOOP) if v <= 0 else v


def replacement_min_market_value_to_replace_usd(rep_cfg: dict[str, Any]) -> float:
    raw = rep_cfg.get("min_market_value_to_replace_usd", 0)
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return 0.0


def replacement_min_notional_for_incoming_usd(rep_cfg: dict[str, Any]) -> float:
    raw = rep_cfg.get("min_notional_for_incoming_usd", 0)
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return 0.0


def replacement_max_per_cycle(rep_cfg: dict[str, Any]) -> int:
    raw = rep_cfg.get("max_replacements_per_entry_cycle", 1)
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 1


def replacement_strength_gap_ok(
    incoming_strength: float,
    weakest_strength: float,
    *,
    threshold: float,
    allow_equal_replacement: bool,
    strength_jitter_max: float,
) -> tuple[bool, str | None]:
    """
    Require incoming signal to be clearly better than weakest holding.

    effective_gap = incoming - weakest
    Must exceed threshold.
    """
    gap = float(incoming_strength) - float(weakest_strength)

    if not allow_equal_replacement and abs(gap) <= float(strength_jitter_max):
        return False, (
            "replacement skipped — signals too close "
            f"(gap {gap:.3f} <= jitter {float(strength_jitter_max):.3f})"
        )

    if gap < float(threshold):
        return False, (
            "replacement skipped — insufficient strength improvement "
            f"(gap {gap:.3f} < threshold {float(threshold):.3f})"
        )

    return True, None


def replacement_size_ok(
    *,
    weakest_market_value_usd: float,
    incoming_notional_usd: float,
    rep_cfg: dict[str, Any],
) -> tuple[bool, str | None]:
    min_replace = replacement_min_market_value_to_replace_usd(rep_cfg)
    min_incoming = replacement_min_notional_for_incoming_usd(rep_cfg)

    if weakest_market_value_usd < min_replace:
        return False, (
            "replacement skipped — weakest position too small "
            f"(${weakest_market_value_usd:.0f} < ${min_replace:.0f})"
        )

    if incoming_notional_usd < min_incoming:
        return False, (
            "replacement skipped — incoming order too small "
            f"(${incoming_notional_usd:.0f} < ${min_incoming:.0f})"
        )

    return True, None


# Live loop: max new entry orders (options or stock) per user pass; add-on stock buys when
# ``portfolio.allow_add`` and symbol already held do not count (see run_alpaca_loop).
MAX_NEW_TRADES_PER_LOOP = 3


def effective_signal_strength(
    base_strength: float,
    jitter_max: float = 0.0,
    *,
    rng: random.Random | None = None,
) -> float:
    """
    ``base_strength + uniform(0, jitter_max)`` when *jitter_max* > 0; else *base_strength* unchanged.

    Used for portfolio replacement ordering and persisted ``signal_strength`` so near-ties
    do not always collide on the same float.
    """
    b = float(base_strength)
    jm = float(jitter_max)
    if jm <= 0.0:
        return b
    gen = rng if rng is not None else random
    return b + float(gen.uniform(0.0, jm))


def allowed_symbols_for_stock_orders_set(portfolio_cfg: dict[str, Any] | None) -> frozenset[str] | None:
    """
    If ``portfolio.allowed_symbols_for_stock_orders`` is present as a list, stock BUY entries are
    restricted to that set (uppercased). If the key is missing, return ``None`` (no restriction).
    An empty list yields an empty frozenset (no symbol passes).
    """
    if not portfolio_cfg:
        return None
    raw = portfolio_cfg.get("allowed_symbols_for_stock_orders")
    if raw is None:
        return None
    if not isinstance(raw, (list, tuple, set)):
        return None
    out = {str(x).strip().upper() for x in raw if x is not None and str(x).strip()}
    return frozenset(out)


def new_symbol_blocked_at_position_cap_only_replacement(
    *,
    max_positions: int,
    enable_replacement: bool,
    current_positions: Mapping[str, Any],
    symbol_upper: str,
    incoming_signal_strength: float | None = None,
    strong_signal_cap_relief_enabled: bool = False,
    strong_signal_min_strength: float = 0.82,
    cap_relief: Mapping[str, Any] | None = None,
    defer_to_ranked_batch: bool = False,
) -> bool:
    """
    New tickers (not in *current_positions*) at the name cap: returns **False** (do not pre-block)
    so :func:`trend_long_ranked_dispatch.dispatch_trend_long_after_buying_power` can evaluate
    rotation: sell weakest (when the incoming signal is stronger) then buy. ``enable_replacement`` is
    not required in config (the flag remains on :func:`new_symbol_blocked_at_position_cap_only_replacement`
    for call-site compatibility and has no effect on the return value).

    *defer_to_ranked_batch* — when true (``allocation.rank_by_signal_strength`` in the live loop),
    returns False at name cap so candidates are collected; the scan ends with
    :func:`rank_trend_long_candidate_rows` keeping the top *N* by strength.

    When ``portfolio.strong_signal_cap_relief`` is enabled and *incoming_signal_strength* qualifies
    (and optional ``relief_symbols`` allow this ticker), returns False (allow one path through despite
    cap — engine/sizing still enforce risk).

    When *cap_relief* is provided (parsed ``strong_signal_cap_relief`` dict), eligibility uses
    :func:`src.exposure_gates.strong_signal_cap_relief_eligible_for_symbol`. Otherwise falls back to
    *strong_signal_cap_relief_enabled* and *strong_signal_min_strength* only (all symbols).
    """
    if max_positions >= 10**9:
        return False
    su = str(symbol_upper).strip().upper()
    if not su:
        return False
    if su in current_positions:
        return False
    try:
        n = len(current_positions)
    except TypeError:
        return False
    if n < int(max_positions):
        return False
    if defer_to_ranked_batch and n >= int(max_positions):
        return False
    if cap_relief is not None and isinstance(cap_relief, dict):
        if strong_signal_cap_relief_eligible_for_symbol(
            cap_relief, symbol_upper=su, entry_strength=incoming_signal_strength
        ):
            return False
    elif (
        strong_signal_cap_relief_enabled
        and incoming_signal_strength is not None
        and float(incoming_signal_strength) >= float(strong_signal_min_strength)
    ):
        return False
    return False


def trend_long_blocked_by_portfolio_cap(
    *,
    max_positions: int,
    enable_replacement: bool,
    allow_add: bool,
    num_eligible_long_stocks: int,
    symbol_upper: str,
    eligible_long_symbols_upper: set[str],
    top_n_batch_mode: bool = False,
    incoming_signal_strength: float | None = None,
    strong_signal_cap_relief_enabled: bool = False,
    strong_signal_min_strength: float = 0.82,
    cap_relief: Mapping[str, Any] | None = None,
) -> bool:
    """
    True when the live loop should skip *symbol_upper* for trend-long entries because the
    portfolio is at the distinct-long cap and this symbol is not an add-on to an existing hold.

    When *top_n_batch_mode* is true, never skip for cap (scan collects signals; top-N flush applies).

    New names at the position cap are **not** short-circuited here: dispatch may rotate the weakest
    (see :func:`trend_long_ranked_dispatch.dispatch_trend_long_after_buying_power`) without requiring
    ``portfolio.enable_replacement`` to be true in config.

    Optional ``portfolio.strong_signal_cap_relief``: when enabled and *incoming_signal_strength*
    qualifies (and optional ``relief_symbols`` allow this ticker), returns False so a strong candidate
    is not skipped early at cap. Pass *cap_relief* from :func:`src.exposure_gates.parse_strong_signal_cap_relief`
    for symbol filtering; otherwise *strong_signal_cap_relief_enabled* / *strong_signal_min_strength* apply
    to all symbols.
    """
    if top_n_batch_mode:
        return False
    if max_positions >= 10**9:
        return False
    if num_eligible_long_stocks < max_positions:
        return False
    if allow_add and symbol_upper in eligible_long_symbols_upper:
        return False
    su = str(symbol_upper).strip().upper()
    if cap_relief is not None and isinstance(cap_relief, dict):
        if strong_signal_cap_relief_eligible_for_symbol(
            cap_relief, symbol_upper=su, entry_strength=incoming_signal_strength
        ):
            return False
    elif (
        strong_signal_cap_relief_enabled
        and incoming_signal_strength is not None
        and float(incoming_signal_strength) >= float(strong_signal_min_strength)
    ):
        return False
    return False


def should_short_circuit_trend_long_symbol_scan(
    *,
    top_n_enabled: bool,
    enable_replacement: bool,
    allow_add: bool,
    broker_position_count: int,
) -> bool:
    """
    True when the live loop should skip the entire trend-long ``for symbol`` iteration.

    Triggers only when broker ``position_count`` (open position rows) is at or above
    :data:`MAX_POSITIONS`. Top-N batch, replacement, and add-on modes still need a full scan.
    """
    if top_n_enabled or allow_add:
        return False
    return False


def eligible_long_stock_symbols(
    positions: list[dict[str, Any]],
    *,
    universe_symbols: set[str],
    bear_etf_symbols: set[str],
) -> list[str]:
    """Listed long equity positions in the trade universe, excluding bear ETFs and options."""
    out: list[str] = []
    seen: set[str] = set()
    for p in positions:
        sym = str(p.get("symbol") or "").strip().upper()
        if not sym or sym in seen:
            continue
        if is_option_symbol(sym):
            continue
        if sym in bear_etf_symbols:
            continue
        if sym not in universe_symbols:
            continue
        if int(float(p.get("qty") or 0)) <= 0:
            continue
        seen.add(sym)
        out.append(sym)
    return out


def tracked_signal_strength(tracked_row: dict[str, Any] | None) -> float:
    """Per-position rotation score: ``signal_strength`` saved at entry; default 1.0 for legacy rows."""
    if not tracked_row:
        return 1.0
    s = tracked_row.get("signal_strength")
    try:
        return float(s) if s is not None else 1.0
    except (TypeError, ValueError):
        return 1.0


def parse_weakest_pick(rep_sub: Mapping[str, Any] | None) -> str:
    """
    Read ``portfolio.replacement.weakest_pick`` (alias ``weakest_definition``).

    * ``entry_signal_strength`` (default) — lowest persisted :func:`tracked_signal_strength`.
    * ``pnl_momentum_trend`` / ``health`` — :func:`hold_health_score_normalized` (mean of three on ``[0,1]``).
    * ``composite_position_score`` and aliases **weakest_simple**, **momentum_pnl_trend_strength_sum**,
      **strength_sum** — :func:`~src.position_scoring.weakest_position_score_momentum_pnl_trend`
      (``momentum + pnl_leg + trend`` on ``[0,3]``; *lowest* = weakest; *pnl* = :func:`pnl_score_01`).

    """
    if not isinstance(rep_sub, dict):
        return WEAKEST_PICK_ENTRY_SIGNAL_STRENGTH
    raw = rep_sub.get("weakest_pick")
    if raw is None or str(raw).strip() == "":
        raw = rep_sub.get("weakest_definition")
    if raw is None or str(raw).strip() == "":
        return WEAKEST_PICK_ENTRY_SIGNAL_STRENGTH
    s = str(raw).strip().lower().replace("-", "_")
    if s in ("pnl_momentum_trend", "pnl_mom_trend", "health", "live_health"):
        return WEAKEST_PICK_PNL_MOMENTUM_TREND
    if s in (
        "composite_position_score",
        "composite_sum",
        "composite",
        "position_composite",
        "min_composite_score",
        "momentum_pnl_trend_strength_sum",
        "momentum_pnl_trend_sum",
        "weakest_simple",
        "strength_sum",
        "three_term_sum",
    ):
        return WEAKEST_PICK_COMPOSITE_POSITION_SCORE
    return WEAKEST_PICK_ENTRY_SIGNAL_STRENGTH


def hold_health_score_normalized(
    symbol_upper: str,
    positions: Sequence[Mapping[str, Any]] | None,
    df: pd.DataFrame | None,
    *,
    ma_slow: int,
    momentum_bars: int,
    volume_bars: int,
) -> float:
    """
    Mean of three ``[0, 1]`` terms — **pnl_score**, **trend_strength**, **momentum_score** — from
    :func:`src.position_scoring.composite_position_score` (sum divided by 3 so the result is ``[0, 1]``
    for comparison with normalized entry :func:`effective_signal_strength`).
    """
    total, _bd = composite_position_score(
        str(symbol_upper).strip().upper(),
        positions,
        df,
        ma_slow=int(ma_slow),
        momentum_bars=int(momentum_bars),
        volume_bars=int(volume_bars),
    )
    return float(total) / 3.0


def _strategy_health_params(strategy: Any) -> tuple[int, int, int]:
    """MA slow + composite rank bar windows from :class:`~src.strategy.TrendFollowingStrategy` when present."""
    if strategy is None:
        return 200, 10, 20
    ms = int(getattr(strategy, "ma_slow", 200) or 200)
    mb_raw = getattr(strategy, "_composite_rank_momentum_bars", None)
    vb_raw = getattr(strategy, "_composite_rank_volume_bars", None)
    mb = int(mb_raw if mb_raw is not None else getattr(strategy, "momentum_bars", 10) or 10)
    vb = int(vb_raw if vb_raw is not None else getattr(strategy, "volume_bars", 20) or 20)
    return ms, mb, vb


def replacement_hold_strength(
    symbol_upper: str,
    tracked: dict[str, Any] | None,
    positions: Sequence[Mapping[str, Any]] | None,
    *,
    get_bars: Callable[[str], Any] | None = None,
    engine: Any | None = None,
    rep_sub: Mapping[str, Any] | None = None,
    weakest_pick: str | None = None,
) -> float:
    """
    Per-line **strength** (higher = stronger) using the same rules as :func:`weakest_replacement_hold`.

    Used to **sort** longs for gross de-lever (lowest value = trim first). When ``weakest_pick`` is
    bar-based, *get_bars* / *engine* are used; otherwise :func:`tracked_signal_strength` for the
    symbol's tracked row (missing row → same default as an empty row).
    """
    su = str(symbol_upper).strip().upper()
    mode = weakest_pick if weakest_pick is not None else parse_weakest_pick(rep_sub)
    use_health = mode == WEAKEST_PICK_PNL_MOMENTUM_TREND and positions is not None and get_bars is not None
    use_composite = (
        mode == WEAKEST_PICK_COMPOSITE_POSITION_SCORE and positions is not None and get_bars is not None
    )
    ms_i, mb_i, vb_i = _strategy_health_params(
        getattr(engine, "strategy", None) if engine is not None else None
    )
    row = (tracked or {}).get(su) if isinstance(tracked, dict) else None
    df_h: pd.DataFrame | None = None
    if use_health or use_composite:
        try:
            raw_b = get_bars(su) if get_bars else None  # type: ignore[misc]
        except Exception:
            raw_b = None
        if raw_b is not None and isinstance(raw_b, pd.DataFrame):
            df_h = raw_b
    if use_composite:
        total_r, _bd = composite_position_score(
            su,
            positions,
            df_h,
            ma_slow=ms_i,
            momentum_bars=mb_i,
            volume_bars=vb_i,
        )
        return float(total_r)
    if use_health:
        return hold_health_score_normalized(
            su, positions, df_h, ma_slow=ms_i, momentum_bars=mb_i, volume_bars=vb_i
        )
    return float(tracked_signal_strength(row if isinstance(row, dict) else None))


def weakest_replacement_hold(
    tracked: dict[str, Any],
    eligible_symbols: list[str],
    *,
    positions: Sequence[Mapping[str, Any]] | None = None,
    get_bars: Callable[[str], Any] | None = None,
    engine: Any | None = None,
    weakest_pick: str | None = None,
    rep_sub: Mapping[str, Any] | None = None,
) -> tuple[str | None, float]:
    """
    ``weakest_position`` for rotation — ``min(portfolio, key=score)`` among eligible holds.

    Default (``weakest_pick`` / ``rep_sub.weakest_pick`` = ``entry_signal_strength``): lowest persisted
    :func:`tracked_signal_strength`; ties broken by symbol name.

    When ``weakest_pick`` is ``pnl_momentum_trend`` and ``positions`` + ``get_bars`` are provided, score is
    the normalized mean ``(pnl + trend_strength + momentum) / 3`` per :func:`hold_health_score_normalized`.

    When ``weakest_pick`` is ``composite_position_score``, score is the raw composite sum in ``[0, 3]`` from
    :func:`position_scoring.composite_position_score` (same components as health, **not** divided by 3).
    Incoming comparison in :func:`src.portfolio_score_replacement.evaluate_strength_based_portfolio_swap`
    uses the same scale for the new candidate when OHLCV is provided.

    If bars cannot be fetched, trend/momentum use neutral 0.5. When dependencies are missing for the health
    / composite modes, falls back to entry-strength picking.
    """
    mode = weakest_pick if weakest_pick else parse_weakest_pick(rep_sub)
    candidates = replacement_hold_candidates_sorted_asc(
        tracked,
        eligible_symbols,
        positions=positions,
        get_bars=get_bars,
        engine=engine,
        rep_sub=rep_sub,
        weakest_pick=mode,
    )
    if not candidates:
        return None, 0.0
    return candidates[0][0], candidates[0][1]


def replacement_hold_candidates_sorted_asc(
    tracked: dict[str, Any],
    eligible_symbols: list[str],
    *,
    positions: Sequence[Mapping[str, Any]] | None = None,
    get_bars: Callable[[str], Any] | None = None,
    engine: Any = None,
    rep_sub: Mapping[str, Any] | None = None,
    weakest_pick: str | None = None,
) -> list[tuple[str, float]]:
    """
    All eligible long lines with replacement strength, sorted **weakest first** (same scoring as
    :func:`weakest_replacement_hold`).

    Used to sell **multiple** laggards for one stronger incoming (``replace_losers_with_winners``).
    """
    mode: str
    if weakest_pick and str(weakest_pick).strip():
        mode = str(weakest_pick).strip().lower().replace("-", "_")
    else:
        mode = parse_weakest_pick(rep_sub)
    use_health = mode == WEAKEST_PICK_PNL_MOMENTUM_TREND and positions is not None and get_bars is not None
    use_composite = (
        mode == WEAKEST_PICK_COMPOSITE_POSITION_SCORE and positions is not None and get_bars is not None
    )
    if mode == WEAKEST_PICK_PNL_MOMENTUM_TREND and not use_health:
        log.debug(
            "weakest_pick=pnl_momentum_trend but positions/get_bars unavailable — using entry_signal_strength"
        )
    if mode == WEAKEST_PICK_COMPOSITE_POSITION_SCORE and not use_composite:
        log.debug(
            "weakest_pick=composite_position_score but positions/get_bars unavailable — using entry_signal_strength"
        )
    candidates: list[tuple[str, float]] = []
    for sym in eligible_symbols:
        su = str(sym).upper()
        row = tracked.get(su)
        if row is None or not tracked_row_has_open_long(row):
            continue
        sc = replacement_hold_strength(
            su,
            tracked,
            positions,
            get_bars=get_bars,
            engine=engine,
            rep_sub=rep_sub,
            weakest_pick=mode,
        )
        candidates.append((su, sc))
    candidates.sort(key=lambda x: (x[1], x[0]))
    return candidates


def _priority_tie_tuple(sym: str, priority: Sequence[str] | None) -> tuple[int, int, str] | None:
    """If *priority* is set, return sortable tie tuple (lower is better). Else None (use lexicographic)."""
    if not priority:
        return None
    pr = [str(x).strip().upper() for x in priority if x and str(x).strip()]
    if not pr:
        return None
    su = str(sym).upper()
    if su in pr:
        return (0, pr.index(su), su)
    return (1, 0, su)


def stronger_deferred_replacement_row(
    incumbent: dict[str, Any] | None,
    candidate: dict[str, Any],
    *,
    strength_key: str = "strength_eff",
    sym_key: str = "sym_u",
    priority_symbols: Sequence[str] | None = None,
) -> dict[str, Any]:
    """
    When at portfolio cap with replacement, defer stock entries until the end of the symbol scan
    and pick the candidate with the highest *strength_key* (ε-tolerant). Ties break on
    *priority_symbols* when provided, else smaller *sym_key* (scan-order neutral).
    """
    if incumbent is None:
        return candidate
    se_c = float(candidate[strength_key])
    se_i = float(incumbent[strength_key])
    eps = 1e-12
    if se_c > se_i + eps:
        return candidate
    if se_i > se_c + eps:
        return incumbent
    pt_c = _priority_tie_tuple(str(candidate[sym_key]), priority_symbols)
    pt_i = _priority_tie_tuple(str(incumbent[sym_key]), priority_symbols)
    if pt_c is not None and pt_i is not None:
        return candidate if pt_c < pt_i else incumbent
    return (
        candidate
        if str(candidate[sym_key]) < str(incumbent[sym_key])
        else incumbent
    )


def replacement_weakest_min_hold_ok(
    *,
    weakest_entry_time_iso: str | None,
    now: datetime,
    min_hold_minutes: float | None = None,
) -> tuple[bool, str | None]:
    """
    Returns ``(True, None)`` if the weakest hold may be rotated out, else ``(False, reason)``.

    Uses :func:`position_tracker.minutes_held` vs :data:`MIN_HOLD_TIME_MINUTES` or *min_hold_minutes*.
    When the configured cap is ``<= 0``, the check is disabled (always ok).
    """
    raw = MIN_HOLD_TIME_MINUTES if min_hold_minutes is None else min_hold_minutes
    try:
        cap = float(raw)
    except (TypeError, ValueError):
        cap = MIN_HOLD_TIME_MINUTES
    if cap <= 0:
        return True, None
    age = minutes_held(str(weakest_entry_time_iso or ""), now)
    if age < cap:
        return (
            False,
            "portfolio replacement: weakest hold %.1f min < min hold %.0f min (skip rotation)"
            % (age, cap),
        )
    return True, None


def replacement_rotate_target_ok(
    *,
    incoming_sym_upper: str,
    decision: Any,
    tracked: dict[str, Any],
    eligible_active: list[str],
    max_port_positions: int,
    rep_sub: dict[str, Any],
    now: datetime,
    replace_if_weakest_older_than_bars: int | None,
    strength_jitter_max: float,
    broker: Any | None = None,
    engine: Any | None = None,
    positions: Sequence[Mapping[str, Any]] | None = None,
) -> tuple[bool, str | None, str | None]:
    """
    Revalidate a deferred replacement row before execution (weakest hold and gates may change
    after a prior replacement in the same loop). Returns ``(True, weakest_sym, None)`` or
    ``(False, None, reason)``.
    """
    if max_port_positions >= 10**9:
        return False, None, "portfolio replacement flush: max_positions unbounded"
    if len(eligible_active) < max_port_positions:
        return False, None, "portfolio replacement flush: below cap"
    su = str(incoming_sym_upper).strip().upper()

    def _gb_rotate(s: str) -> Any:
        if broker is None:
            return None
        try:
            return broker.get_bars(s, timeframe="1Day", limit=220)
        except Exception:
            return None

    _wsym, _wstr = weakest_replacement_hold(
        tracked,
        eligible_active,
        positions=positions,
        get_bars=_gb_rotate if broker is not None else None,
        engine=engine,
        rep_sub=rep_sub,
    )
    if _wsym is None:
        return False, None, "portfolio replacement: no eligible hold to rotate"
    if _wsym == su:
        return False, None, "portfolio replacement: incoming is weakest"
    _w_row_rep = tracked.get(_wsym) or {}
    _w_entry_iso = _w_row_rep.get("entry_time")
    _mh_raw = rep_sub.get("min_hold_minutes")
    _mh_override: float | None
    if _mh_raw is None or str(_mh_raw).strip() == "":
        _mh_override = None
    else:
        try:
            _mh_override = float(_mh_raw)
        except (TypeError, ValueError):
            _mh_override = None
    _ok_mh, _mh_reason = replacement_weakest_min_hold_ok(
        weakest_entry_time_iso=str(_w_entry_iso) if _w_entry_iso else None,
        now=now,
        min_hold_minutes=_mh_override,
    )
    if not _ok_mh:
        return False, None, _mh_reason or "portfolio replacement: min hold not met"
    _w_age_bars = bars_held(str(_w_entry_iso), now) if _w_entry_iso else None
    _entry_str = (
        float(decision.entry_signal.strength) if decision.entry_signal else 1.0
    )
    _entry_eff = effective_signal_strength(_entry_str, strength_jitter_max)
    if not replacement_strength_ok(
        _entry_eff,
        _wstr,
        weakest_age_bars=_w_age_bars,
        replace_if_weakest_older_than_bars=replace_if_weakest_older_than_bars,
    ):
        return False, None, "portfolio replacement flush: strength vs weakest no longer ok"
    return True, _wsym, None


def replacement_strength_ok(
    new_strength: float,
    weakest_strength: float,
    *,
    weakest_age_bars: int | None = None,
    replace_if_weakest_older_than_bars: int | None = None,
) -> bool:
    """
    True if the weakest hold is stale (*weakest_age_bars* > *replace_if_weakest_older_than_bars*
    when that threshold is configured), else if *new_strength* > *weakest_strength* (ε-tolerant).
    """
    if replace_if_weakest_older_than_bars is not None and weakest_age_bars is not None:
        if int(weakest_age_bars) > int(replace_if_weakest_older_than_bars):
            return True
    n = float(new_strength)
    w = float(weakest_strength)
    eps = 1e-9
    return n > w + eps
