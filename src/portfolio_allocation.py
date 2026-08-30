"""
Portfolio sleeve caps: limit how much equity is allocated to options vs stock (market value basis).

Defaults match a 40% / 60% split of **account equity** for **open long** exposure (options = OCC
symbols; stock = everything else). Override via ``portfolio.max_options_capital_pct`` and
``portfolio.max_stock_capital_pct`` (0–100).

**Cash reserve:** keep at least ``portfolio.min_cash_reserve_pct`` (default **10%**) of
account equity as a planning floor — buying power used for new stock entries is
``min(raw_buying_power, equity × (1 − reserve))``. Override via ``portfolio.min_cash_reserve_pct``
(decimal fraction or percent points). When ``cash_management.reserve_by_regime`` is set, the
**effective** reserve (used by :func:`effective_buying_power_for_entries` when a regime is passed)
comes from the ``bullish`` / ``neutral`` / ``bearish`` (and optional ``defensive`` for engine
``"defensive"``) table; else :func:`min_cash_reserve_frac` applies. Optional
``rebalance.trigger`` (merged from top-level and ``portfolio``) can restrict proactive trims to
``signal_deterioration`` / ``low_cash`` / etc.; see :func:`parse_rebalance_sell_triggers` and
``config/default.yaml``. ``portfolio.rebalance_each_cycle`` with ``portfolio.min_cash_target_pct``
lifts cash only when the trigger set allows (legacy: unchanged). ``portfolio.rebalance_tolerance_pct`` (percent points) slack below target.
(decimal ``0.10`` = 10% fraction, or ``10`` / ``20`` = percent points on a 0–100 scale).

Options premium sizing uses ``options.total_exposure_limit`` (fraction or percent) or legacy
``options.max_total_options_exposure_pct``; the **effective** ceiling is ``min(that, portfolio sleeve %)``.
Optional ``portfolio.capital_split`` partitions **deployable** BP (after min-cash reserve) between stock
allocator / equity flows vs options premium sizing — see :func:`capital_split_stock_option_fracs` /
:func:`scaled_buying_power_for_lane`.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

# Same OCC rule as options_premium_risk (avoid importing that module — circular import).
_OCC_OPTION_RE = re.compile(r"^[A-Z]{1,6}\d{6}[CP]\d{8}$")


def _is_option_symbol(symbol: str) -> bool:
    s = str(symbol or "").strip().upper().replace(" ", "")
    if not s or len(s) < 12:
        return False
    return bool(_OCC_OPTION_RE.match(s))

# Defaults: max fraction of account equity in each sleeve (open long market value + proposed add).
MAX_OPTIONS_CAPITAL_FRAC = 0.40
MAX_STOCK_CAPITAL_FRAC = 0.60
# Minimum fraction of account equity to treat as reserved (not available for new entry sizing vs BP).
MIN_CASH_RESERVE_FRAC = 0.10


def _pct_to_frac(raw: Any, *, default_frac: float) -> float:
    if raw is None or str(raw).strip() == "":
        return max(0.0, min(1.0, float(default_frac)))
    try:
        return max(0.0, min(1.0, float(raw) / 100.0))
    except (TypeError, ValueError):
        return max(0.0, min(1.0, float(default_frac)))


def effective_options_total_cap_frac(config: dict[str, Any]) -> float:
    """
    Fraction of equity for total long-options premium budget (existing + new),
    from ``options.total_exposure_limit`` (preferred) or ``options.max_total_options_exposure_pct``,
    combined with ``portfolio.max_options_capital_pct`` via the stricter value.
    """
    opts = config.get("options") or {}
    yaml_frac = 0.05
    tel = opts.get("total_exposure_limit")
    if tel is None or str(tel).strip() == "":
        tel = opts.get("max_options_notional_pct")
    raw = opts.get("max_total_options_exposure_pct")
    if tel is not None and str(tel).strip() != "":
        try:
            v = float(tel)
            yaml_frac = v / 100.0 if v > 1.0 else v
        except (TypeError, ValueError):
            yaml_frac = 0.05
    elif raw is not None and str(raw).strip() != "":
        try:
            yaml_frac = float(raw) / 100.0
        except (TypeError, ValueError):
            yaml_frac = 0.05
    yaml_frac = max(0.0, min(1.0, yaml_frac))
    port = config.get("portfolio") or {}
    sleeve = _pct_to_frac(port.get("max_options_capital_pct"), default_frac=MAX_OPTIONS_CAPITAL_FRAC)
    return min(yaml_frac, sleeve)


def effective_stock_capital_frac(config: dict[str, Any]) -> float:
    """
    Max fraction of equity in long **stock** market value (non-OCC symbols), including a new buy.

    When ``portfolio.allocator.mode: dynamic_risk_budget`` is on, the rigid
    ``max_stock_capital_pct`` is **not** used: the stock sleeve is ``1 −`` :func:`effective_options_total_cap_frac`
    (options + stock partition equity; bucket targets describe how risk is *split*, not a fixed 60% cap).
    """
    from src.dynamic_risk_budget import dynamic_risk_budget_enabled

    if dynamic_risk_budget_enabled(config):
        opt = effective_options_total_cap_frac(config)
        return max(0.0, min(1.0, 1.0 - float(opt)))
    port = config.get("portfolio") or {}
    return _pct_to_frac(port.get("max_stock_capital_pct"), default_frac=MAX_STOCK_CAPITAL_FRAC)


def _parse_portfolio_percent_points(raw: Any) -> float:
    """Parse ``25``, ``25.0``, or ``\"25%\"`` as percent of equity (0–100+); invalid → ``0``."""
    if raw is None or str(raw).strip() == "":
        return 0.0
    s = str(raw).strip()
    if s.endswith("%"):
        s = s[:-1].strip()
    try:
        return max(0.0, float(s))
    except (TypeError, ValueError):
        return 0.0


def symbol_allocation_cap_is_dynamic(raw: Any) -> bool:
    """True when ``portfolio.symbol_allocation_cap`` selects the equity-aware dynamic cap."""
    if raw is None:
        return False
    if isinstance(raw, str) and raw.strip().lower() == "dynamic":
        return True
    if isinstance(raw, dict) and str(raw.get("mode", "")).strip().lower() == "dynamic":
        return True
    return False


def dynamic_symbol_allocation_cap_pct(
    *,
    account_equity: float,
    dyn: Mapping[str, Any] | None,
) -> float:
    """
    Single-name cap % of equity from ``portfolio.symbol_allocation_cap_dynamic``.

    ``min( max_pct, max( floor_pct, 100 × min_trade_size_usd / equity ) )`` so small accounts
    keep enough headroom for *min_trade_size_usd* while large accounts use at least *floor_pct*
    up to *max_pct*.
    """
    d = dict(dyn or {})
    try:
        max_pct = float(d.get("max_pct", 30))
    except (TypeError, ValueError):
        max_pct = 30.0
    max_pct = max(0.0, min(100.0, max_pct))
    try:
        min_usd = float(d.get("min_trade_size_usd", 500))
    except (TypeError, ValueError):
        min_usd = 500.0
    min_usd = max(0.0, min_usd)
    try:
        floor_pct = float(d.get("floor_pct", 15))
    except (TypeError, ValueError):
        floor_pct = 15.0
    floor_pct = max(0.0, min(100.0, floor_pct))
    if account_equity <= 0:
        return max_pct
    need_pct = 100.0 * min_usd / float(account_equity)
    return min(max_pct, max(floor_pct, need_pct))


def max_allocation_per_symbol_pct(
    config: dict[str, Any] | None,
    *,
    account_equity: float | None = None,
) -> float:
    """
    Percent of account equity allowed in a single long equity symbol (live skip gate).

    Reads ``portfolio.symbol_allocation_cap``, then ``portfolio.max_allocation_per_symbol``,
    then ``portfolio.max_single_position_pct`` when the first two are unset.

    * Static: ``25``, ``\"25%\"``, etc.
    * **dynamic** — uses ``portfolio.symbol_allocation_cap_dynamic`` and *account_equity*
      (pass ``account_equity`` from the live loop / sizer / portfolio brain). When equity is
      unknown, uses ``max_pct`` only (ceiling) so gates stay conservative in tests.
    """
    port = (config or {}).get("portfolio") or {}
    raw = port.get("symbol_allocation_cap")
    if raw is None or str(raw).strip() == "":
        raw = port.get("max_allocation_per_symbol")
    if raw is None or str(raw).strip() == "":
        raw = port.get("max_single_position_pct")
    if symbol_allocation_cap_is_dynamic(raw):
        sub = port.get("symbol_allocation_cap_dynamic")
        eq = float(account_equity) if account_equity is not None else 0.0
        return dynamic_symbol_allocation_cap_pct(
            account_equity=eq,
            dyn=sub if isinstance(sub, dict) else {},
        )
    return _parse_portfolio_percent_points(raw)


def symbol_long_position_market_value_usd(
    positions: list[dict[str, Any]] | None, sym_upper: str
) -> float:
    """Abs ``market_value`` for a long equity row matching *sym_upper* (OCC symbols ignored)."""
    if not positions:
        return 0.0
    su = str(sym_upper or "").strip().upper()
    for p in positions:
        sym = str(p.get("symbol") or "").strip().upper()
        if sym != su or not sym or _is_option_symbol(sym):
            continue
        try:
            qty = int(float(p.get("qty") or 0))
        except (TypeError, ValueError):
            qty = 0
        if qty <= 0:
            continue
        try:
            return abs(float(p.get("market_value") or 0))
        except (TypeError, ValueError):
            return 0.0
    return 0.0


def symbol_position_has_headroom_below_cap(
    sym_upper: str,
    *,
    positions: list[dict[str, Any]] | None,
    account_equity: float,
    max_alloc_sym_pct: float,
    max_pos_mval_usd: float,
) -> bool:
    """
    True when the symbol's long equity market value is strictly under the effective %%-of-equity
    cap (headroom for add-ons) and not above ``entries.max_position_market_value_usd`` when set.

    Used by the live loop so ``allow_add_on_strong_momentum`` does not treat ``allow_add`` as true
    when the name is already at/above its allocation ceiling.
    """
    mv = symbol_long_position_market_value_usd(positions, sym_upper)
    if float(max_pos_mval_usd) > 0 and mv > float(max_pos_mval_usd) + 1e-6:
        return False
    eq = float(account_equity)
    cap_pct = float(max_alloc_sym_pct)
    if cap_pct <= 0.0 or eq <= 0.0:
        return True
    cap_usd = eq * (cap_pct / 100.0)
    return mv < cap_usd - 1e-6


def symbol_long_unrealized_pl_pct(
    sym_upper: str,
    *,
    positions: list[dict[str, Any]] | None,
) -> float | None:
    """
    Unrealized P&L as **percent of cost basis** for a long equity row (not OCC options).

    Uses broker ``unrealized_pl`` / ``cost_basis`` when present; otherwise
    ``market_value − avg×qty`` vs cost when ``avg_entry_price`` / ``avg_price`` and ``qty`` exist.
    Returns ``None`` when the symbol is missing or basis cannot be determined.
    """
    su = str(sym_upper or "").strip().upper()
    if not su:
        return None
    for p in positions or []:
        sym = str(p.get("symbol") or "").strip().upper()
        if sym != su or _is_option_symbol(sym):
            continue
        ur_raw = p.get("unrealized_pl")
        cb_raw = p.get("cost_basis")
        try:
            if cb_raw is not None and str(cb_raw).strip() != "":
                cbf = abs(float(cb_raw))
                if cbf > 1e-9 and ur_raw is not None and str(ur_raw).strip() != "":
                    return 100.0 * float(ur_raw) / cbf
        except (TypeError, ValueError):
            pass
        try:
            mv = abs(float(p.get("market_value") or 0))
        except (TypeError, ValueError):
            mv = 0.0
        try:
            qty = int(float(p.get("qty") or 0))
        except (TypeError, ValueError):
            qty = 0
        if qty <= 0 or mv <= 0:
            return None
        apx = p.get("avg_entry_price")
        if apx is None or str(apx).strip() == "":
            apx = p.get("avg_price")
        try:
            avg = float(apx) if apx is not None and str(apx).strip() != "" else 0.0
        except (TypeError, ValueError):
            avg = 0.0
        cost = abs(avg * float(qty))
        if cost <= 1e-9:
            return None
        return 100.0 * (mv - cost) / cost
    return None


def parse_pyramid_into_winners_cfg(
    portfolio_cfg: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """
    ``portfolio.pyramid_into_winners``: allow pyramiding adds when already at/near the per-name
    **%% cap** if strong trend + minimum unrealized profit (live loop wires this with
    ``allow_add_on_strong_momentum``).
    """
    dflt: dict[str, Any] = {
        "enabled": False,
        "min_unrealized_profit_pct": 5.0,
        "cap_relax_multiplier": 1.15,
    }
    if not isinstance(portfolio_cfg, dict):
        return dict(dflt)
    raw = portfolio_cfg.get("pyramid_into_winners")
    if not isinstance(raw, dict):
        return dict(dflt)
    en = raw.get("enabled", False)
    if isinstance(en, str):
        enabled = str(en).strip().lower() not in ("0", "false", "no", "off", "")
    else:
        enabled = bool(en) if en is not None else False
    try:
        mup = float(raw.get("min_unrealized_profit_pct", dflt["min_unrealized_profit_pct"]))
    except (TypeError, ValueError):
        mup = float(dflt["min_unrealized_profit_pct"])
    mup = max(0.0, mup)
    try:
        crm = float(raw.get("cap_relax_multiplier", dflt["cap_relax_multiplier"]))
    except (TypeError, ValueError):
        crm = float(dflt["cap_relax_multiplier"])
    crm = max(1.0, min(crm, 3.0))
    return {
        "enabled": enabled,
        "min_unrealized_profit_pct": mup,
        "cap_relax_multiplier": crm,
    }


def sum_long_stock_market_value_usd(positions: list[dict[str, Any]] | None) -> float:
    """Sum ``market_value`` for long equity rows (excludes OCC option symbols)."""
    if not positions:
        return 0.0
    total = 0.0
    for p in positions:
        sym = str(p.get("symbol") or "").strip().upper()
        if not sym or _is_option_symbol(sym):
            continue
        try:
            qty = int(float(p.get("qty") or 0))
        except (TypeError, ValueError):
            qty = 0
        if qty <= 0:
            continue
        try:
            total += abs(float(p.get("market_value") or 0))
        except (TypeError, ValueError):
            pass
    return total


def sum_long_option_market_value_usd(positions: list[dict[str, Any]] | None) -> float:
    """Sum ``market_value`` for long OCC option rows (informational; premium caps use cost basis)."""
    if not positions:
        return 0.0
    total = 0.0
    for p in positions:
        sym = str(p.get("symbol") or "").strip().upper()
        if not sym or not _is_option_symbol(sym):
            continue
        try:
            qty = int(float(p.get("qty") or 0))
        except (TypeError, ValueError):
            qty = 0
        if qty <= 0:
            continue
        try:
            total += abs(float(p.get("market_value") or 0))
        except (TypeError, ValueError):
            pass
    return total


def stock_buy_within_capital_sleeve(
    *,
    equity: float,
    positions: list[dict[str, Any]] | None,
    additional_stock_notional: float,
    config: dict[str, Any],
) -> tuple[bool, str | None]:
    """
    True if ``existing stock MV + additional_notional`` does not exceed the stock sleeve cap.
    """
    if equity <= 0:
        return False, "equity <= 0"
    cap = float(equity) * effective_stock_capital_frac(config)
    current = sum_long_stock_market_value_usd(positions)
    add = max(0.0, float(additional_stock_notional))
    if current + add > cap + 1e-6:
        return (
            False,
            "stock capital cap (existing $%.0f + new $%.0f > sleeve $%.0f, %.0f%% of equity $%.0f)"
            % (
                current,
                add,
                cap,
                effective_stock_capital_frac(config) * 100.0,
                equity,
            ),
        )
    return True, None


def min_cash_reserve_frac(config: dict[str, Any] | None) -> float:
    """
    Fraction of equity reserved (not counted toward deployable BP cap); default 10%.

    ``portfolio.min_cash_reserve_pct``:

    * Strictly between ``0`` and ``1`` (e.g. ``0.10``) → treated as a **fraction** of equity.
    * Otherwise (e.g. ``10``, ``20``) → **percent points** on a 0–100 scale (``10`` → 10%).
    """
    port = (config or {}).get("portfolio") or {}
    raw = port.get("min_cash_reserve_pct")
    if raw is None or str(raw).strip() == "":
        return max(0.0, min(1.0, float(MIN_CASH_RESERVE_FRAC)))
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return max(0.0, min(1.0, float(MIN_CASH_RESERVE_FRAC)))
    if 0.0 < v < 1.0:
        return max(0.0, min(1.0, v))
    return max(0.0, min(1.0, v / 100.0))


def _parse_cash_pct_to_frac(raw: Any) -> float | None:
    """One ``min_cash_reserve_pct``-style value to ``[0,1]``; ``None`` if missing/invalid."""
    if raw is None or str(raw).strip() == "":
        return None
    s = str(raw).strip()
    if s.endswith("%"):
        try:
            return max(0.0, min(1.0, float(s[:-1].strip()) / 100.0))
        except (TypeError, ValueError):
            return None
    try:
        v = float(s)
    except (TypeError, ValueError):
        return None
    if 0.0 < v < 1.0:
        return max(0.0, min(1.0, v))
    return max(0.0, min(1.0, v / 100.0))


def _regime_leg_for_cash_reserve(
    regime_condition: str | None, regime_score: int | None
) -> str | None:
    """``bullish`` / ``neutral`` / ``bearish`` for :func:`effective_min_cash_reserve_frac` table lookup."""
    c = (regime_condition or "").strip().lower() if regime_condition else None
    if c == "defensive":
        c = "bearish"
    if c in ("bullish", "neutral", "bearish"):
        return c
    if regime_score is not None:
        try:
            s = int(regime_score)
        except (TypeError, ValueError):
            return None
        if s >= 4:
            return "bullish"
        if s >= 2:
            return "neutral"
        return "bearish"
    return None


def effective_min_cash_reserve_frac(
    config: dict[str, Any] | None,
    *,
    regime_condition: str | None = None,
    regime_score: int | None = None,
    full_invest: bool = False,
) -> float:
    """
    Min cash **reserve** (fraction of equity) for deployable-BP capping, possibly by regime.

    If *full_invest* (from ``execution.strong_signals_count`` in the live loop when the wave
    has enough high-strength names), returns ``0`` so deployable BP is only broker- and equity-bounded.

    When ``portfolio.allocator.mode: dynamic_risk_budget`` is on, the fixed
    ``portfolio.min_cash_reserve_pct`` and regime table are **bypassed**; use
    ``portfolio.allocator.cash_buffer_pct`` via :func:`src.dynamic_risk_budget.effective_drb_cash_buffer_frac`.

    If ``cash_management.reserve_by_regime`` is a non-empty dict, reads the ``bullish`` /
    ``neutral`` / ``bearish`` entry (and ``defensive`` as a synonym of ``bearish``). Otherwise
    returns :func:`min_cash_reserve_frac` (or when regime cannot be resolved).
    """
    if full_invest:
        return 0.0
    from src.dynamic_risk_budget import dynamic_risk_budget_enabled, effective_drb_cash_buffer_frac

    if dynamic_risk_budget_enabled(config):
        return float(effective_drb_cash_buffer_frac(config))
    cm = (config or {}).get("cash_management")
    cm = cm if isinstance(cm, dict) else {}
    rbr = cm.get("reserve_by_regime")
    if not isinstance(rbr, dict) or not rbr:
        return min_cash_reserve_frac(config)
    leg = _regime_leg_for_cash_reserve(regime_condition, regime_score)
    if leg is None:
        return min_cash_reserve_frac(config)
    rawv = rbr.get(leg)
    if (rawv is None or str(rawv).strip() == "") and leg == "bearish":
        rawv = rbr.get("defensive")
    p = _parse_cash_pct_to_frac(rawv)
    if p is None:
        return min_cash_reserve_frac(config)
    return p


def min_cash_target_frac(config: dict[str, Any] | None) -> float:
    """
    Target minimum broker **cash** / equity (fraction in ``[0, 1]``) for ``portfolio.rebalance_each_cycle``.

    Reads ``portfolio.min_cash_target_pct`` with the same rules as :func:`min_cash_reserve_frac`.
    Returns ``0`` when unset (proactive cash rebalance uses no target).
    """
    port = (config or {}).get("portfolio") or {}
    raw = port.get("min_cash_target_pct")
    if raw is None or str(raw).strip() == "":
        return 0.0
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return 0.0
    if v <= 0.0:
        return 0.0
    if 0.0 < v < 1.0:
        return max(0.0, min(1.0, v))
    return max(0.0, min(1.0, v / 100.0))


def portfolio_rebalance_each_cycle(config: dict[str, Any] | None) -> bool:
    """True when ``portfolio.rebalance_each_cycle`` requests a proactive cash lift each entry scan."""
    port = (config or {}).get("portfolio") or {}
    return bool(port.get("rebalance_each_cycle", False))


def portfolio_rebalance_tolerance_pct(config: dict[str, Any] | None) -> float:
    """
    Percentage-point slack (0–100 scale) below ``min_cash_target_pct`` before ``rebalance_each_cycle`` trims.

    Target cash %% of equity is ``min_cash_target_frac × 100``. With tolerance ``T``, the live loop
    trims only when cash %% < target − ``T``. ``0`` means trim whenever cash is strictly below target.
    """
    port = (config or {}).get("portfolio") or {}
    if not isinstance(port, dict):
        return 0.0
    raw = port.get("rebalance_tolerance_pct")
    if raw is None or str(raw).strip() == "":
        return 0.0
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, v)


def rebalance_sub_config(config: dict[str, Any] | None) -> dict[str, Any]:
    """
    Merge ``portfolio.rebalance`` then top-level ``rebalance``; later keys win (typical user override).
    """
    out: dict[str, Any] = {}
    c = config or {}
    port = c.get("portfolio")
    if isinstance(port, dict):
        r = port.get("rebalance")
        if isinstance(r, dict):
            out.update(r)
    r2 = c.get("rebalance")
    if isinstance(r2, dict):
        out.update(r2)
    return out


def _rebalance_parse_trigger_tokens(raw: Any) -> set[str]:
    if raw is None or str(raw).strip() == "":
        return set()
    s = str(raw)
    # OR / comma / semicolon: "A OR b OR c"
    parts = re.split(r"\s+OR\s+|\s*[,;|]\s*", s, flags=re.IGNORECASE)
    out: set[str] = set()
    for p in parts:
        t = p.strip().lower().replace(" ", "_").replace("-", "_")
        if not t:
            continue
        if t in ("and", "not"):
            continue
        out.add(t)
    return out


# Normalized reason tokens in ``rebalance.trigger`` (see :func:`parse_rebalance_sell_triggers`).
_REB_TOK_MIN_CASH = frozenset({"low_cash", "min_cash", "min_cash_target", "cash_floor", "min_cash_target_trim"})
_REB_TOK_SIG_DET = frozenset({"signal_deterioration", "deterioration", "signal_weakening", "weakening"})
_REB_TOK_STRONGER = frozenset(
    {
        "stronger_incoming",
        "stronger_incoming_replace",
        "rotate",
        "rotation",
        "replace_with_stronger",
    }
)
# stop_loss / take_profit: documented for alignment with the exit system; not used to enable REB paths.


@dataclass(frozen=True)
class RebalanceSellTriggers:
    """
    When *legacy* (no ``rebalance.trigger`` in merged config), live ``run_alpaca_loop`` rfc and
    min-cash behavior match historical flags only.

    When a trigger string is set, proactive **min_cash** trims require a ``low_cash`` (etc.) token;
    **partial** rfc trims require ``signal_deterioration``. **Buying-power** full exit of the weakest
    line when the incoming signal is stronger is controlled only by
    ``rebalance_free_capital.rotate_full_weakest_when_stronger`` in ``run_alpaca_loop`` (not by
    ``stronger_incoming`` / ``rotate`` trigger tokens).
    """

    legacy: bool
    allow_min_cash_target_trim: bool
    allow_rfc_partial_trim: bool
    require_rfc_deterioration: bool
    allow_rfc_full_stronger_incoming: bool


def parse_rebalance_sell_triggers(config: dict[str, Any] | None) -> RebalanceSellTriggers:
    """
    Parse top-level and/or ``portfolio`` ``rebalance.trigger`` (e.g. ``"signal_deterioration OR low_cash"``).

    ``stop_loss`` and ``take_profit`` are pass-through documentation tokens (exits own those paths).
    """
    sub = rebalance_sub_config(config)
    raw = sub.get("trigger")
    if raw is None or str(raw).strip() == "":
        return RebalanceSellTriggers(
            legacy=True,
            allow_min_cash_target_trim=True,
            allow_rfc_partial_trim=True,
            require_rfc_deterioration=False,
            allow_rfc_full_stronger_incoming=True,
        )
    raw_toks = _rebalance_parse_trigger_tokens(raw)
    toks: set[str] = set()
    for t in raw_toks:
        toks.add(t)
        if t in _REB_TOK_MIN_CASH:
            toks.update(_REB_TOK_MIN_CASH)
        if t in _REB_TOK_SIG_DET:
            toks.update(_REB_TOK_SIG_DET)
        if t in _REB_TOK_STRONGER:
            toks.update(_REB_TOK_STRONGER)
    has_mc = not _REB_TOK_MIN_CASH.isdisjoint(toks)
    has_det = not _REB_TOK_SIG_DET.isdisjoint(toks)
    has_str = not _REB_TOK_STRONGER.isdisjoint(toks)
    return RebalanceSellTriggers(
        legacy=False,
        allow_min_cash_target_trim=has_mc,
        allow_rfc_partial_trim=has_det,
        require_rfc_deterioration=has_det,
        allow_rfc_full_stronger_incoming=has_str,
    )


def rebalance_signal_deterioration_min_gap(config: dict[str, Any] | None) -> float:
    """
    Min absolute drop on the ``[0,1]`` hold-health scale vs entry :func:`tracked_signal_strength`
    before a **partial** rfc trim is allowed (when :func:`parse_rebalance_sell_triggers` requires it).
    """
    sub = rebalance_sub_config(config)
    raw = sub.get("signal_deterioration_min_gap", 0.05)
    try:
        g = float(raw)
    except (TypeError, ValueError):
        return 0.05
    return max(0.0, min(0.5, g))


def effective_buying_power_for_entries(
    *,
    buying_power: float,
    equity: float,
    config: dict[str, Any] | None,
    regime_condition: str | None = None,
    regime_score: int | None = None,
    full_invest: bool = False,
) -> float:
    """
    Buying power available for **new** stock orders after applying min cash reserve vs equity.

    ``min(broker_buying_power, equity × (1 − reserve))`` when equity > 0. *reserve* is
    :func:`effective_min_cash_reserve_frac` (``cash_management.reserve_by_regime`` when set and
    *regime_* can be resolved, else :func:`min_cash_reserve_frac`), unless *full_invest* (see
    that function). Pass *regime_score* / *regime_condition* from the live market-regime result when available.
    """
    bp = max(0.0, float(buying_power))
    eq = max(0.0, float(equity))
    if eq <= 0:
        return bp
    reserve = effective_min_cash_reserve_frac(
        config,
        regime_condition=regime_condition,
        regime_score=regime_score,
        full_invest=full_invest,
    )
    deployable = eq * max(0.0, min(1.0, 1.0 - reserve))
    return max(0.0, min(bp, deployable))


def _parse_capital_split_fraction(raw: Any) -> float | None:
    """Parse ``0.7``, ``70``, ``\"70%\"`` → fraction in ``[0, 1]``; invalid → ``None``."""
    if raw is None:
        return None
    if isinstance(raw, str):
        s = str(raw).strip()
        if not s:
            return None
        pct = s.endswith("%")
        if pct:
            s = s[:-1].strip()
        try:
            v = float(s)
        except (TypeError, ValueError):
            return None
        if pct or v > 1.0 + 1e-9:
            return max(0.0, min(1.0, v / 100.0))
        return max(0.0, min(1.0, v))
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    if v > 1.0 + 1e-9:
        return max(0.0, min(1.0, v / 100.0))
    return max(0.0, min(1.0, v))


def capital_split_stock_option_fracs(config: Mapping[str, Any] | None) -> tuple[float, float]:
    """
    Return ``(stocks_frac, options_frac)`` to partition **deployable** buying power (after min-cash
    reserve) between equity entries / stock allocator vs options premium budget sizing.

    Config: ``portfolio.capital_split`` with ``enabled`` (default ``False`` when section missing —
    legacy single pool ``(1.0, 1.0)``), ``stocks`` / ``options`` as fraction or percent string.
    When only one side is set, the other is ``1 − side``. When enabled and both missing, ``0.5`` / ``0.5``.
    When both set, values are renormalized to sum to ``1`` if their sum differs materially.
    """
    if not config:
        return 1.0, 1.0
    port = config.get("portfolio")
    if not isinstance(port, dict):
        return 1.0, 1.0
    cs = port.get("capital_split")
    if not isinstance(cs, dict):
        return 1.0, 1.0
    en_raw = cs.get("enabled", False)
    if isinstance(en_raw, str):
        enabled = en_raw.strip().lower() not in ("0", "false", "no", "off", "")
    else:
        enabled = bool(en_raw)
    if not enabled:
        return 1.0, 1.0
    st = _parse_capital_split_fraction(cs.get("stocks"))
    op = _parse_capital_split_fraction(cs.get("options"))
    if st is None and op is None:
        return 0.5, 0.5
    if st is not None and op is None:
        op = max(0.0, min(1.0, 1.0 - float(st)))
    elif op is not None and st is None:
        st = max(0.0, min(1.0, 1.0 - float(op)))
    st_f = float(st)
    op_f = float(op)
    ssum = st_f + op_f
    if ssum > 1e-12 and abs(ssum - 1.0) > 1e-6:
        st_f, op_f = st_f / ssum, op_f / ssum
    return max(0.0, min(1.0, st_f)), max(0.0, min(1.0, op_f))


def scaled_buying_power_for_lane(
    *,
    buying_power: float,
    equity: float,
    config: dict[str, Any] | None,
    regime_condition: str | None = None,
    regime_score: int | None = None,
    full_invest: bool = False,
    lane: Literal["stocks", "options"],
) -> float:
    """
    Deployable BP for *lane* after min-cash reserve and optional ``portfolio.capital_split``.

    Multiplies :func:`effective_buying_power_for_entries` by the stocks or options fraction from
    :func:`capital_split_stock_option_fracs` when ``capital_split.enabled``; otherwise unchanged.
    """
    base = effective_buying_power_for_entries(
        buying_power=buying_power,
        equity=equity,
        config=config,
        regime_condition=regime_condition,
        regime_score=regime_score,
        full_invest=full_invest,
    )
    sm, om = capital_split_stock_option_fracs(config)
    m = sm if lane == "stocks" else om
    return max(0.0, float(base) * float(m))


def cash_pct_of_equity(*, cash: float | None, equity: float) -> float | None:
    """
    Broker cash as a percent of account equity (0–100 scale), or ``None`` if unknown.

    Uses ``max(0, cash)`` so negative cash does not read as a high idle-cash ratio.
    """
    if cash is None:
        return None
    eq = float(equity)
    if eq <= 0:
        return None
    return 100.0 * max(0.0, float(cash)) / eq


def parse_add_on_gate_cfg(portfolio_cfg: Mapping[str, Any] | None) -> dict[str, Any]:
    """
    Parse ``portfolio.add_on`` — optional extra gates for **add-ons** only (symbol already held).

    Keys:

    * ``enabled`` — when false (default), no extra checks.
    * ``min_signal_strength`` — require ``entry_signal.strength`` **strictly greater** than this (0–1 scale).
    * ``max_scaled_position_usd`` (alias ``max_scaled_size_usd``) — allow add only while long MV is
      **strictly below** this USD amount; ``0`` disables the size gate.
    * ``incremental_add_pct`` — max add notional as a fraction of equity for an existing symbol
      (default ``0.02`` = 2%).
    """
    if not isinstance(portfolio_cfg, dict):
        portfolio_cfg = {}
    raw = portfolio_cfg.get("add_on")
    raw = raw if isinstance(raw, dict) else {}
    enabled = bool(raw.get("enabled", False))
    ms_raw = raw.get("min_signal_strength")
    try:
        min_strength = (
            float(ms_raw)
            if ms_raw is not None and str(ms_raw).strip() != ""
            else None
        )
    except (TypeError, ValueError):
        min_strength = None
    if min_strength is not None:
        min_strength = max(0.0, min(1.0, float(min_strength)))

    mx_raw = raw.get("max_scaled_position_usd")
    if mx_raw is None or str(mx_raw).strip() == "":
        mx_raw = raw.get("max_scaled_size_usd")
    try:
        max_scaled_usd = (
            float(mx_raw)
            if mx_raw is not None and str(mx_raw).strip() != ""
            else 0.0
        )
    except (TypeError, ValueError):
        max_scaled_usd = 0.0
    max_scaled_usd = max(0.0, float(max_scaled_usd))
    inc_raw = raw.get("incremental_add_pct")
    if inc_raw is None or str(inc_raw).strip() == "":
        inc_raw = raw.get("max_incremental_add_pct")
    if inc_raw is None or str(inc_raw).strip() == "":
        inc_raw = raw.get("small_add_pct")
    if inc_raw is None or str(inc_raw).strip() == "":
        inc_raw = raw.get("allow_add_small_pct")
    try:
        incremental_add_pct = (
            float(inc_raw)
            if inc_raw is not None and str(inc_raw).strip() != ""
            else 0.02
        )
    except (TypeError, ValueError):
        incremental_add_pct = 0.02
    if incremental_add_pct > 1.0 + 1e-9:
        incremental_add_pct = incremental_add_pct / 100.0
    incremental_add_pct = max(0.0, min(1.0, float(incremental_add_pct)))

    return {
        "enabled": enabled,
        "min_signal_strength": min_strength,
        "max_scaled_position_usd": max_scaled_usd,
        "incremental_add_pct": incremental_add_pct,
    }


def add_on_passes_signal_and_scale(
    *,
    gate_cfg: Mapping[str, Any],
    entry_signal_strength: float | None,
    position_market_value_usd: float,
) -> tuple[bool, float, str | None]:
    """
    Apply :func:`parse_add_on_gate_cfg` output for one candidate add-on.

    Returns ``(allowed, size_multiplier, reason)``.

    When the signal-strength gate is enabled and the add-on is below the preferred threshold,
    scale the add size instead of hard-rejecting:

    ``size_multiplier = signal_strength / min_signal_strength``

    clamped to ``[0, 1]``. Structural gates like ``max_scaled_position_usd`` still reject.
    """
    if not isinstance(gate_cfg, dict) or not bool(gate_cfg.get("enabled")):
        return True, 1.0, None

    thr = gate_cfg.get("min_signal_strength")
    if thr is not None:
        try:
            t = float(thr)
        except (TypeError, ValueError):
            t = 0.0
        try:
            st = float(entry_signal_strength) if entry_signal_strength is not None else float("nan")
        except (TypeError, ValueError):
            st = float("nan")
        if st != st:
            return False, 0.0, "add-on blocked — signal_strength unavailable"
        if t > 1e-12 and st <= t:
            scale = max(0.0, min(1.0, float(st) / float(t)))
            return (
                True,
                scale,
                "add-on scaled — signal_strength %.3f below threshold %.3f"
                % (st, t),
            )

    max_usd = float(gate_cfg.get("max_scaled_position_usd") or 0.0)
    if max_usd > 0.0:
        pv = max(0.0, float(position_market_value_usd))
        if pv >= max_usd - 1e-9:
            return (
                False,
                0.0,
                "add-on blocked — position $%.0f >= max_scaled_position_usd $%.0f"
                % (pv, max_usd),
            )

    return True, 1.0, None


def is_high_cash_deploy(config: dict[str, Any] | None, *, cash: float | None, equity: float) -> bool:
    """
    True when ``cash / equity × 100`` is at least ``portfolio.high_cash_deploy_pct``.

    ``high_cash_deploy_pct`` of ``0`` or missing disables the check.
    """
    port = (config or {}).get("portfolio") or {}
    raw = port.get("high_cash_deploy_pct", 0)
    try:
        thr = float(raw) if raw is not None and str(raw).strip() != "" else 0.0
    except (TypeError, ValueError):
        thr = 0.0
    if thr <= 0:
        return False
    pct = cash_pct_of_equity(cash=cash, equity=float(equity))
    return pct is not None and pct >= thr
