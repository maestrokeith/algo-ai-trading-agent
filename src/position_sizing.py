"""
Position sizing: risk budget vs volatility (ATR), symbol cap remaining, conviction, portfolio heat.

**Volatility sizing** (when ``position_sizing.volatility_sizing.enabled`` and ATR% is available):
``shares = min( risk_budget / (atr_dollar × atr_risk_multiple), max_symbol_cap_remaining / price )``
where ``atr_dollar = price × (atr_pct / 100)``. Higher ATR → smaller size (e.g. NVDA vs SPY at the
same risk budget). When disabled or ATR missing, falls back to stop-distance sizing
(``risk / (price × stop_pct/100)``).

**Risk budget** scales by optional conviction (entry signal strength in ``[0, 1]``) and optional portfolio heat
(long stock MV vs equity) before the min() above.

``volatility_sizing.signal_strength_mapping``:

- ``affine`` — map strength linearly between ``conviction_min_scale`` and ``conviction_max_scale`` (legacy default).
- ``proportional`` — risk multiplier ``≈ strength`` with ``signal_strength_floor`` as minimum (allocation ∝ strength).

Optional **confidence sizing** (YAML ``confidence_sizing.enabled``) multiplies share count by
``trend_strength + momentum_strength + volume_signal`` (0–3) after the risk/cap merge when callers pass
``ohlcv_df``. When ``suppress_when_signal_strength_proportional`` is true and mapping is proportional, that
step is skipped to avoid double-scaling overlap with composite entry strength.

- ``max_position_pct``: optional single-name notional cap as a **floor** (fraction ``0.08`` = 8%%) vs the merged
  ``portfolio``/``risk`` per-symbol cap from :func:`effective_symbol_allocation_cap_pct`. The **larger** of
  the two (floor vs effective/fallback) wins for the sizer so a tight ``risk.max_symbol_allocation_pct`` does
  not zero out add-ons on ~25K books (pair with ``max_exposure_per_symbol_pct`` when the merged cap is off and
  you use only the ``position_sizing`` block).
- Max notional per symbol: min(% of equity, optional max_position_dollar_cap) minus existing.
- Max exposure per sector: clip share count to remaining sector headroom (same idea as symbol cap).

:class:`PositionSizingResult` includes ``exposure_pct`` (sized notional / equity) and optional
``trimmed`` / ``trim_reason`` when size was reduced by symbol cap, high-vol haircut, or regime multiplier.
Optional ``position_sizing.small_fill`` can bump 0-share sizing to 1 share when
``remaining_capacity_usd >= min_trade_value`` (and capacity covers one share at price); otherwise small fill is skipped.

**Micro cap relief** (``position_sizing.micro_cap_relief``): when symbol-cap **remaining USD** is positive
but below one share at *price* (integer ``shares_cap`` is 0 while risk-based size ≥ 1), optionally **ignore**
the symbol cap for that pass so the order is not rejected with ``symbol cap remaining yields zero shares``.
``mode: one_share`` (default) allows a single share; ``full_risk`` sets the cap ceiling to the full risk-based
share count. Rebalance / trim remains handled upstream in the live loop.

Optional ``position_sizing.min_position_pct`` / ``target_position_pct`` (fraction or percent points, same rules as
``strategy.exits.target_position_pct``): after risk, confidence, and dynamic multipliers, bump whole-share count up to
at least ``max(min, target) × equity / price``, capped by the symbol-cap share ceiling (see :meth:`PositionSizer.size_position`).

``portfolio.max_sector_pct`` and ``risk.sector_cap_pct`` merge (stricter wins); fallback
``position_sizing.max_exposure_per_sector_pct`` (see :func:`~src.risk_limits.effective_max_sector_sleeve_pct`).
``position_sizing.min_trade_dollars`` is an alias for ``small_fill.min_trade_value`` when that nested key is omitted.

Portfolio-level notional caps (``portfolio.max_gross_exposure`` / ``max_gross_exposure_pct``, or if both
omitted ``portfolio.target_gross_exposure_pct`` — same fraction/%% rules — then ``position_sizing.max_gross_exposure_pct``,
``max_net_exposure_pct``, etc.) are applied
via :meth:`PositionSizer._cap_notional_by_portfolio_budget` when callers supply current exposure %%.

**Theme / sector caps** (``position_sizing.sector_caps``): optional per-bucket %% of equity ceilings
for the *theme* leg of that budget step (same buckets as ``src.exposure.THEME_MAP``). Keys
``broad_index``, ``financials``, … apply to that theme; ``tech`` sets one cap for
``ai_growth``, ``semis``, and ``high_beta_growth``; ``others`` is the fallback for themes
not listed. Values use the same convention as ``max_theme_exposure_pct`` (``60`` = 60%%;
``0.35`` = 35%%). When ``sector_caps`` is absent, ``max_theme_exposure_pct`` applies to all themes.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

from src.exposure import THEME_MAP
from src.portfolio_allocation import _regime_leg_for_cash_reserve
from src.options_premium_risk import is_option_symbol
from src.signal_ranking import confidence_score_trend_momentum_volume
from src.risk_limits import (
    effective_max_sector_sleeve_pct,
    effective_symbol_allocation_cap_pct,
    parse_allocation_fraction,
)
from src.sector_config import parse_sector_config, resolve_sector_key_for_sizing
from src.strategy import target_position_pct_to_fraction


@dataclass
class PositionSizingResult:
    shares: int
    notional: float
    risk_amount: float
    risk_pct: float
    exposure_pct: float = 0.0
    trimmed: bool = False
    trim_reason: str | None = None
    reject_reason: str | None = None
    confidence_score: float | None = None


def clamp_position_sizing_to_max_buy_usd(
    sizing: PositionSizingResult,
    *,
    max_buy_usd: float,
    price: float,
    account_equity: float,
    extra_trim: str = "portfolio_soft_cap",
) -> PositionSizingResult:
    """
    Lower shares/notional to fit *max_buy_usd* when soft-capping near portfolio gross/symbol gates.
    """
    if sizing.reject_reason:
        return sizing
    try:
        px = float(price)
        cap = float(max_buy_usd)
        eq = float(account_equity)
    except (TypeError, ValueError):
        return sizing
    if px <= 1e-12 or cap <= 1e-12:
        return PositionSizingResult(
            shares=0,
            notional=0.0,
            risk_amount=0.0,
            risk_pct=0.0,
            reject_reason="soft_cap: no buy headroom under cap",
        )
    old_sh = int(sizing.shares)
    if old_sh <= 0:
        return sizing
    new_sh = max(0, int(cap / px))
    if new_sh >= old_sh:
        return sizing
    if new_sh < 1:
        return PositionSizingResult(
            shares=0,
            notional=0.0,
            risk_amount=0.0,
            risk_pct=0.0,
            reject_reason="soft_cap: clamped size below 1 share",
        )
    scale = float(new_sh) / float(old_sh)
    notional = new_sh * px
    risk_amount = float(sizing.risk_amount) * scale
    risk_pct = (risk_amount / eq) * 100.0 if eq > 1e-12 else 0.0
    exposure_pct = (notional / eq) * 100.0 if eq > 1e-12 else 0.0
    parts: list[str] = []
    if sizing.trim_reason:
        parts.append(str(sizing.trim_reason))
    parts.append(extra_trim)
    return PositionSizingResult(
        shares=new_sh,
        notional=notional,
        risk_amount=risk_amount,
        risk_pct=risk_pct,
        exposure_pct=exposure_pct,
        trimmed=True,
        trim_reason="; ".join(parts),
        reject_reason=None,
        confidence_score=sizing.confidence_score,
    )


def scale_position_sizing_by_multiplier(
    sizing: PositionSizingResult,
    mult: float,
    *,
    trim_reason: str,
    price: float,
    account_equity: float,
) -> PositionSizingResult:
    """Reduce shares/notional/risk by *mult* (e.g. liquid-symbol spread wider than tier cap)."""
    if sizing.reject_reason:
        return sizing
    try:
        m = float(mult)
        px = float(price)
        eq = float(account_equity)
    except (TypeError, ValueError):
        return sizing
    if m >= 1.0 - 1e-12 or px <= 1e-12:
        return sizing
    m = max(0.0, min(1.0, m))
    old_sh = int(sizing.shares)
    if old_sh <= 0:
        return sizing
    new_sh = max(0, int(old_sh * m))
    if new_sh < 1:
        return PositionSizingResult(
            shares=0,
            notional=0.0,
            risk_amount=0.0,
            risk_pct=0.0,
            exposure_pct=0.0,
            reject_reason=f"{trim_reason}: scaled below 1 share",
        )
    scale = float(new_sh) / float(old_sh)
    notional = new_sh * px
    risk_amount = float(sizing.risk_amount) * scale
    risk_pct = (risk_amount / eq) * 100.0 if eq > 1e-12 else 0.0
    exposure_pct = (notional / eq) * 100.0 if eq > 1e-12 else 0.0
    parts: list[str] = []
    if sizing.trim_reason:
        parts.append(str(sizing.trim_reason))
    parts.append(trim_reason)
    return PositionSizingResult(
        shares=new_sh,
        notional=notional,
        risk_amount=risk_amount,
        risk_pct=risk_pct,
        exposure_pct=exposure_pct,
        trimmed=True,
        trim_reason="; ".join(parts),
        reject_reason=None,
        confidence_score=sizing.confidence_score,
    )


def _long_stock_mv_from_positions_dict(current_positions: dict[str, Any] | None) -> float:
    """Sum equity (non-OCC) notionals from ``current_positions`` values."""
    total = 0.0
    for sym, row in (current_positions or {}).items():
        if is_option_symbol(str(sym)):
            continue
        try:
            total += abs(float((row or {}).get("notional", 0) or 0))
        except (TypeError, ValueError):
            continue
    return total


_SECTOR_CAPS_TECH_THEMES = frozenset({"ai_growth", "semis", "high_beta_growth"})


def _parse_theme_cap_pct_value(raw: Any) -> float | None:
    """Percent of equity (``60`` → 60%%); values in ``(0, 1]`` are treated as fractions."""
    if raw is None or str(raw).strip() == "":
        return None
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    if v <= 1.0:
        return v * 100.0
    return min(100.0, v)


def _parse_sector_caps_theme_limits(ps: dict[str, Any]) -> tuple[dict[str, float], float | None]:
    """
    Build per-theme cap (%% equity) and optional ``others`` default from ``sector_caps`` YAML.

    Returns ``({}, None)`` when unset.
    """
    raw = ps.get("sector_caps")
    if not isinstance(raw, dict) or not raw:
        return {}, None
    per_theme: dict[str, float] = {}
    others: float | None = None
    for k, v in raw.items():
        nk = str(k).strip().lower()
        pct = _parse_theme_cap_pct_value(v)
        if pct is None:
            continue
        if nk == "tech":
            for tt in _SECTOR_CAPS_TECH_THEMES:
                per_theme[tt] = float(pct)
        elif nk == "others":
            others = float(pct)
        else:
            per_theme[nk] = float(pct)
    return per_theme, others


def _symbol_row_notional(symbol: str, current_positions: dict[str, Any] | None) -> float:
    su = str(symbol or "").strip().upper()
    for k, row in (current_positions or {}).items():
        if str(k).strip().upper() != su:
            continue
        try:
            return abs(float((row or {}).get("notional", 0) or 0))
        except (TypeError, ValueError):
            return 0.0
    return 0.0


class PositionSizer:
    def __init__(self, config: dict[str, Any]):
        self._app_config = config
        ps = config.get("position_sizing", {})
        self.risk_per_trade_pct = float(ps.get("risk_per_trade_pct", 0.5))
        self.max_open_risk_pct = float(ps.get("max_open_risk_pct", 3.0))
        raw_me = ps.get("max_exposure_per_symbol_pct")
        if raw_me is None or str(raw_me).strip() == "":
            raw_me = ps.get("max_symbol_exposure_pct")
        raw_mp = ps.get("max_position_pct")
        if raw_me is not None and str(raw_me).strip() != "":
            self.max_exposure_per_symbol_pct = float(raw_me)
        elif raw_mp is not None and str(raw_mp).strip() != "":
            self.max_exposure_per_symbol_pct = float(parse_allocation_fraction(raw_mp) * 100.0)
        else:
            self.max_exposure_per_symbol_pct = 20.0
        # Optional floor: max(effective or fallback, max_position_pct) in percent points (see size_position).
        if raw_mp is not None and str(raw_mp).strip() != "":
            self._symbol_cap_floor_pp = float(parse_allocation_fraction(raw_mp) * 100.0)
        else:
            self._symbol_cap_floor_pp = 0.0
        leader = config.get("leader_overrides") or {}
        self.leader_override_symbols: set[str] = {
            str(s).strip().upper()
            for s in (leader.get("symbols") or [])
            if s is not None and str(s).strip()
        }
        _leader_cap = leader.get("max_symbol_exposure_pct")
        try:
            self.leader_max_symbol_exposure_pct = (
                float(_leader_cap)
                if _leader_cap is not None and str(_leader_cap).strip() != ""
                else None
            )
        except (TypeError, ValueError):
            self.leader_max_symbol_exposure_pct = None
        cap_raw = ps.get("max_position_dollar_cap")
        self.max_position_dollar_cap: float | None = (
            float(cap_raw) if cap_raw is not None and cap_raw != "" else None
        )
        self.max_exposure_per_sector_pct = float(effective_max_sector_sleeve_pct(config))
        _ssec = parse_sector_config(config)
        self._sector_default = str(_ssec.get("default_sector", "unknown"))
        self._sector_enforce_caps_on_unknown = bool(_ssec.get("enforce_caps_on_unknown", True))
        hvr = ps.get("high_vol_reduction", {})
        self.high_vol_enabled = bool(hvr.get("enabled", False))
        self.high_vol_atr_threshold = float(hvr.get("atr_pct_threshold", 2.0))
        self.high_vol_size_multiplier = float(hvr.get("size_multiplier", 0.5))
        mr = config.get("market_regime") or {}
        _ahv = mr.get("allow_high_volatility_min_regime_score")
        if _ahv is not None and str(_ahv).strip() != "":
            self.allow_high_volatility_min_regime_score = int(_ahv)
        else:
            self.allow_high_volatility_min_regime_score = None

        vs = ps.get("volatility_sizing") or {}
        self.volatility_sizing_enabled = bool(vs.get("enabled", False))
        self.atr_risk_multiple = float(vs.get("atr_risk_multiple", 1.0))
        if self.atr_risk_multiple <= 0:
            self.atr_risk_multiple = 1.0
        self.conviction_min_scale = float(vs.get("conviction_min_scale", 1.0))
        self.conviction_max_scale = float(vs.get("conviction_max_scale", 1.0))
        _ssm = str(vs.get("signal_strength_mapping", "affine") or "affine").strip().lower()
        self.signal_strength_mapping = _ssm if _ssm in ("affine", "proportional") else "affine"
        try:
            self.signal_strength_floor = max(0.0, float(vs.get("signal_strength_floor", 0.05)))
        except (TypeError, ValueError):
            self.signal_strength_floor = 0.05

        ph = ps.get("portfolio_heat") or {}
        self.portfolio_heat_enabled = bool(ph.get("enabled", False))
        self.portfolio_heat_max_frac = float(ph.get("max_exposure_frac_for_heat", 0.60))
        self.portfolio_heat_min_risk_scale = float(ph.get("min_risk_scale_at_full_heat", 0.25))

        ds = ps.get("dynamic_sizing") or {}
        self.dynamic_sizing_enabled = bool(ds.get("enabled", False))
        dd = ds.get("drawdown_scaling") or {}
        self._dd_mult_at_3 = float(dd.get("dd_3_pct", 0.85))
        self._dd_mult_at_5 = float(dd.get("dd_5_pct", 0.70))
        self._dd_mult_at_8 = float(dd.get("dd_8_pct", 0.50))
        cv = ds.get("conviction_scaling") or {}
        self._conv_mult_strong = float(cv.get("strong", 1.15))
        self._conv_mult_weak = float(cv.get("weak", 0.70))
        sh = ds.get("strategy_health_scaling") or {}
        self._health_cold_thr = float(sh.get("cold_threshold_winrate", 0.40))
        self._health_hot_thr = float(sh.get("hot_threshold_winrate", 0.60))
        self._health_cold_mult = float(sh.get("cold_multiplier", 0.75))
        self._health_hot_mult = float(sh.get("hot_multiplier", 1.10))

        # Optional portfolio-level cap (fraction 0–1 or percent >1); overrides position_sizing.max_gross_exposure_pct.
        # Precedence: max_gross_exposure → max_gross_exposure_pct → target_gross_exposure_pct (operator target book).
        port = config.get("portfolio") or {}
        mg_port = port.get("max_gross_exposure")
        if mg_port is None or str(mg_port).strip() == "":
            mg_port = port.get("max_gross_exposure_pct")
        if mg_port is None or str(mg_port).strip() == "":
            mg_port = port.get("target_gross_exposure_pct")
        if mg_port is not None and str(mg_port).strip() != "":
            try:
                v = float(mg_port)
            except (TypeError, ValueError):
                v = -1.0
            if v > 0:
                self.max_gross_exposure_pct = v * 100.0 if v <= 1.0 else v
            else:
                self.max_gross_exposure_pct = float(ps.get("max_gross_exposure_pct", 100.0))
        else:
            self.max_gross_exposure_pct = float(ps.get("max_gross_exposure_pct", 100.0))
        self._bullish_s4p_max_gross_pct: float | None = None
        _raw_bs4 = port.get("bullish_score_4_plus_max_gross_exposure_pct")
        if _raw_bs4 is not None and str(_raw_bs4).strip() != "":
            try:
                vv = float(_raw_bs4)
                self._bullish_s4p_max_gross_pct = vv * 100.0 if vv <= 1.0 else vv
            except (TypeError, ValueError):
                self._bullish_s4p_max_gross_pct = None
        self.max_net_exposure_pct = float(ps.get("max_net_exposure_pct", 100.0))
        self.max_theme_exposure_pct = float(ps.get("max_theme_exposure_pct", 100.0))
        self._theme_sector_cap_pct, self._theme_sector_cap_others = _parse_sector_caps_theme_limits(ps)
        self.max_etf_exposure_pct = float(ps.get("max_etf_exposure_pct", 100.0))
        self.max_inverse_etf_exposure_pct = float(ps.get("max_inverse_etf_exposure_pct", 100.0))
        from src.dynamic_risk_budget import dynamic_risk_budget_enabled, hedge_bucket_target_pct

        if dynamic_risk_budget_enabled(self._app_config):
            _h = hedge_bucket_target_pct(self._app_config)
            if _h is not None:
                self.max_inverse_etf_exposure_pct = max(0.0, min(100.0, float(_h)))

        sf = ps.get("small_fill") or {}
        self.small_fill_enabled = bool(sf.get("enabled", True))
        _raw_mt = sf.get("min_trade_value")
        if _raw_mt is None or str(_raw_mt).strip() == "":
            _raw_mt = sf.get("min_trade_value_usd")
        if _raw_mt is None or str(_raw_mt).strip() == "":
            _raw_mt = ps.get("min_trade_dollars")
        if _raw_mt is not None and str(_raw_mt).strip() != "":
            try:
                self.small_fill_min_trade_value_usd = max(0.0, float(_raw_mt))
            except (TypeError, ValueError):
                self.small_fill_min_trade_value_usd = 500.0
        else:
            self.small_fill_min_trade_value_usd = 500.0

        _mcr = ps.get("micro_cap_relief")
        if isinstance(_mcr, dict):
            _en_mcr = _mcr.get("enabled", True)
            if isinstance(_en_mcr, str):
                self.micro_cap_relief_enabled = (
                    str(_en_mcr).strip().lower() not in ("0", "false", "no", "off", "")
                )
            else:
                self.micro_cap_relief_enabled = bool(_en_mcr) if _en_mcr is not None else True
            _mode = str(_mcr.get("mode", "one_share") or "one_share").strip().lower()
            self.micro_cap_relief_mode = _mode if _mode in ("one_share", "full_risk") else "one_share"
        else:
            self.micro_cap_relief_enabled = True
            self.micro_cap_relief_mode = "one_share"

        cs = ps.get("confidence_sizing") or {}
        self.confidence_sizing_enabled = bool(cs.get("enabled", False))
        self.confidence_suppress_when_proportional = bool(
            cs.get("suppress_when_signal_strength_proportional", True)
        )
        strat_tf = (config.get("strategy") or {}).get("trend_following") or {}
        try:
            self._conf_ma_slow = int(cs["ma_slow"]) if cs.get("ma_slow") is not None else int(strat_tf.get("ma_slow", 200))
        except (TypeError, ValueError):
            self._conf_ma_slow = 200
        try:
            self._conf_momentum_bars = int(cs.get("momentum_bars", 10))
        except (TypeError, ValueError):
            self._conf_momentum_bars = 10
        try:
            self._conf_volume_bars = int(cs.get("volume_bars", 20))
        except (TypeError, ValueError):
            self._conf_volume_bars = 20

        self.min_position_fraction = target_position_pct_to_fraction(ps.get("min_position_pct"))
        self.target_position_fraction = target_position_pct_to_fraction(ps.get("target_position_pct"))

    def _bump_shares_for_min_target_position_pct(
        self,
        shares: int,
        shares_cap: int,
        account_equity: float,
        price: float,
    ) -> tuple[int, bool]:
        """
        Raise *shares* toward ``max(min_position_fraction, target_position_fraction) × equity`` in shares,
        capped by *shares_cap*. Returns ``(shares, bumped)``.
        """
        eff = max(float(self.min_position_fraction), float(self.target_position_fraction))
        if eff <= 0.0:
            return int(shares), False
        cap = int(shares_cap)
        if cap < 1:
            return int(shares), False
        px = float(price)
        eq = float(account_equity)
        if px <= 0.0 or eq <= 0.0:
            return int(shares), False
        floor_sh = int(eq * eff / px)
        if floor_sh < 1:
            return int(shares), False
        sh = int(shares)
        new_sh = min(cap, max(sh, floor_sh))
        return new_sh, new_sh != sh

    def _small_fill_one_share_ok(self, capacity_usd: float, price: float) -> bool:
        """
        True when we may force 1 share: remaining capacity (USD) meets ``small_fill.min_trade_value``
        and covers one share at *price*. If ``remaining < min_trade_value``, small fill is skipped.
        """
        if not self.small_fill_enabled:
            return False
        px = float(price)
        if px <= 0:
            return False
        rem = float(capacity_usd)
        mt = float(self.small_fill_min_trade_value_usd)
        return rem + 1e-9 >= mt and rem + 1e-9 >= px

    def _theme_portfolio_cap_pct(self, theme_key: str | None) -> float:
        """Max theme book %% of equity for portfolio budget trim (``sector_caps`` or legacy single cap)."""
        if self._theme_sector_cap_pct or self._theme_sector_cap_others is not None:
            t = str(theme_key or "").strip().lower()
            cap = self._theme_sector_cap_pct.get(t)
            if cap is not None:
                return float(cap)
            if self._theme_sector_cap_others is not None:
                return float(self._theme_sector_cap_others)
        return float(self.max_theme_exposure_pct)

    def effective_max_gross_exposure_pct(
        self,
        regime_condition: str | None,
        regime_score: int | None,
    ) -> float:
        """
        Gross MV/equity %% ceiling for portfolio budget; may rise above ``max_gross_exposure_pct`` when
        ``portfolio.bullish_score_4_plus_max_gross_exposure_pct`` is set, regime score ≥ 4, and the
        resolved regime leg is **bullish** (see :func:`~src.portfolio_allocation._regime_leg_for_cash_reserve`).
        """
        base = float(self.max_gross_exposure_pct)
        if self._bullish_s4p_max_gross_pct is None:
            return base
        if regime_score is None:
            return base
        try:
            rs = int(regime_score)
        except (TypeError, ValueError):
            return base
        if rs < 4:
            return base
        if _regime_leg_for_cash_reserve(regime_condition, regime_score) != "bullish":
            return base
        return max(base, float(self._bullish_s4p_max_gross_pct))

    def _cap_notional_by_portfolio_budget(
        self,
        account_equity: float,
        proposed_notional: float,
        current_gross_exposure_pct: float,
        current_net_exposure_pct: float,
        current_theme_exposure_pct: float,
        is_etf: bool,
        is_inverse_etf: bool,
        theme_key: str | None = None,
        *,
        regime_condition: str | None = None,
        regime_score: int | None = None,
    ) -> tuple[float, bool, str | None]:
        """
        Cap *proposed_notional* (USD) using configured %% of equity ceilings vs current exposure %%.

        Gross / net / theme use **remaining** headroom in dollars:
        ``(max_pct - current_pct) / 100 * equity``. ETF / inverse-ETF legs use the YAML caps as a
        fraction of *total* equity (see ``max_etf_exposure_pct`` / ``max_inverse_etf_exposure_pct``).
        """
        max_gross = float(
            self.effective_max_gross_exposure_pct(regime_condition, regime_score)
        )
        max_net = float(self.max_net_exposure_pct)
        max_theme = float(self._theme_portfolio_cap_pct(theme_key))
        max_etf = float(self.max_etf_exposure_pct)
        max_inverse = float(self.max_inverse_etf_exposure_pct)

        allowed_notional = float(proposed_notional)
        trimmed = False
        reason: str | None = None

        gross_room = max(0.0, (max_gross - current_gross_exposure_pct) / 100.0 * account_equity)
        allowed_notional = min(allowed_notional, gross_room)

        net_room = max(0.0, (max_net - current_net_exposure_pct) / 100.0 * account_equity)
        allowed_notional = min(allowed_notional, net_room)

        theme_room = max(0.0, (max_theme - current_theme_exposure_pct) / 100.0 * account_equity)
        allowed_notional = min(allowed_notional, theme_room)

        if is_etf:
            etf_room = max(0.0, (max_etf / 100.0) * account_equity)
            allowed_notional = min(allowed_notional, etf_room)

        if is_inverse_etf:
            inv_room = max(0.0, (max_inverse / 100.0) * account_equity)
            allowed_notional = min(allowed_notional, inv_room)

        if allowed_notional < proposed_notional:
            trimmed = True
            reason = "portfolio_budget_trim"

        return max(0.0, allowed_notional), trimmed, reason

    def portfolio_budget_ceiling_additional_buy_usd(
        self,
        account_equity: float,
        current_gross_exposure_pct: float,
        current_net_exposure_pct: float,
        current_theme_exposure_pct: float,
        is_etf: bool,
        is_inverse_etf: bool,
        theme_key: str | None = None,
        *,
        regime_condition: str | None = None,
        regime_score: int | None = None,
    ) -> float:
        """
        Maximum additional buy USD allowed by gross/net/theme/ETF portfolio budget caps (same envelope as
        :meth:`_cap_notional_by_portfolio_budget`), used for soft-cap retry when sizing returned zero.
        """
        allowed, _, _ = self._cap_notional_by_portfolio_budget(
            account_equity,
            float("inf"),
            current_gross_exposure_pct,
            current_net_exposure_pct,
            current_theme_exposure_pct,
            is_etf,
            is_inverse_etf,
            theme_key=theme_key,
            regime_condition=regime_condition,
            regime_score=regime_score,
        )
        return max(0.0, float(allowed))

    def _apply_dynamic_multipliers(
        self,
        shares: int,
        current_drawdown_pct: float | None = None,
        conviction: str | None = None,
        strategy_winrate: float | None = None,
    ) -> int:
        """
        Scale *shares* by drawdown / conviction label / strategy win-rate when
        ``position_sizing.dynamic_sizing.enabled`` is true.

        Drawdown thresholds use fixed -3%% / -5%% / -8%% (most stressed first); multipliers come from YAML
        ``drawdown_scaling`` (``dd_3_pct``, ``dd_5_pct``, ``dd_8_pct`` — stored as multipliers, not %% points).
        When disabled, returns *shares* unchanged.
        """
        if shares <= 0:
            return 0
        if not self.dynamic_sizing_enabled:
            return shares

        mult = 1.0

        if current_drawdown_pct is not None:
            if current_drawdown_pct <= -8.0:
                mult *= self._dd_mult_at_8
            elif current_drawdown_pct <= -5.0:
                mult *= self._dd_mult_at_5
            elif current_drawdown_pct <= -3.0:
                mult *= self._dd_mult_at_3

        if conviction == "strong":
            mult *= self._conv_mult_strong
        elif conviction == "weak":
            mult *= self._conv_mult_weak

        if strategy_winrate is not None:
            if strategy_winrate < self._health_cold_thr:
                mult *= self._health_cold_mult
            elif strategy_winrate > self._health_hot_thr:
                mult *= self._health_hot_mult

        return max(1, int(shares * mult))

    def _conviction_scale(self, conviction_score: float | None) -> float:
        if conviction_score is None:
            return 1.0
        try:
            c = float(conviction_score)
        except (TypeError, ValueError):
            return 1.0
        if c > 1.0:
            if c <= 100.0:
                c = c / 100.0
            else:
                c = 1.0
        c = max(0.0, min(1.0, c))
        if self.signal_strength_mapping == "proportional":
            return max(self.signal_strength_floor, c)
        lo = min(self.conviction_min_scale, self.conviction_max_scale)
        hi = max(self.conviction_min_scale, self.conviction_max_scale)
        return lo + c * (hi - lo)

    def _portfolio_heat_risk_scale(self, account_equity: float, current_positions: dict[str, Any]) -> float:
        if not self.portfolio_heat_enabled or account_equity <= 0:
            return 1.0
        if self.portfolio_heat_max_frac <= 0:
            return 1.0
        m = _long_stock_mv_from_positions_dict(current_positions)
        denom = float(account_equity) * self.portfolio_heat_max_frac
        heat = min(1.0, max(0.0, m / denom))
        floor_scale = max(0.0, min(1.0, self.portfolio_heat_min_risk_scale))
        return 1.0 - heat * (1.0 - floor_scale)

    def size_position(
        self,
        account_equity: float,
        price: float,
        stop_distance_pct: float,
        symbol: str,
        current_positions: dict[str, Any],
        sector_exposure_pct: dict[str, float],
        symbol_sector: dict[str, str] | None = None,
        atr_pct: float | None = None,
        regime_size_multiplier: float | None = None,
        regime_score: int | None = None,
        conviction_score: float | None = None,
        *,
        current_drawdown_pct: float | None = None,
        conviction: str | None = None,
        strategy_winrate: float | None = None,
        current_gross_exposure_pct: float = 0.0,
        current_net_exposure_pct: float = 0.0,
        current_theme_exposure_pct: float = 0.0,
        is_etf: bool = False,
        is_inverse_etf: bool = False,
        ohlcv_df: pd.DataFrame | None = None,
        theme_key: str | None = None,
        cap_relax_factor: float = 1.0,
        regime_condition: str | None = None,
    ) -> PositionSizingResult:
        """
        Compute shares from risk budget, volatility (ATR or stop %), and symbol cap remaining.

        ``conviction_score``: optional 0–1 (values >1 and ≤100 treated as percent). When omitted,
        callers may rely on defaults (1.0) or pass entry signal strength from the engine.

        When ``position_sizing.confidence_sizing.enabled`` and ``ohlcv_df`` is provided,
        ``shares ← round(shares × (trend_strength + momentum_strength + volume_signal))`` after
        risk/cap merge (each subscore in ``[0, 1]``, same construction as composite ranking).

        Optional ``current_drawdown_pct`` / ``conviction`` / ``strategy_winrate`` feed
        :meth:`_apply_dynamic_multipliers` when ``dynamic_sizing.enabled``. Optional portfolio
        exposure %% and ETF flags feed :meth:`_cap_notional_by_portfolio_budget` after base sizing.

        ``theme_key``: optional :class:`~src.exposure.THEME_MAP` bucket; defaults from *symbol*.
        """
        if account_equity <= 0 or price <= 0:
            return PositionSizingResult(
                shares=0,
                notional=0.0,
                risk_amount=0.0,
                risk_pct=0.0,
                reject_reason="invalid account_equity or price",
            )
        trim_parts: list[str] = []

        risk_budget_base = account_equity * (self.risk_per_trade_pct / 100.0)
        conv = self._conviction_scale(conviction_score)
        heat = self._portfolio_heat_risk_scale(account_equity, current_positions)
        risk_budget = risk_budget_base * conv * heat

        use_atr_path = (
            self.volatility_sizing_enabled
            and atr_pct is not None
            and atr_pct == atr_pct
            and float(atr_pct) > 0
        )
        if use_atr_path:
            atr_dollar = float(price) * (float(atr_pct) / 100.0) * self.atr_risk_multiple
            if atr_dollar <= 0:
                use_atr_path = False
            else:
                risk_per_share = atr_dollar
                shares_by_risk = int(risk_budget / atr_dollar)

        if not use_atr_path:
            if stop_distance_pct <= 0:
                return PositionSizingResult(
                    shares=0,
                    notional=0.0,
                    risk_amount=0.0,
                    risk_pct=0.0,
                    reject_reason="invalid stop_distance_pct",
                )
            risk_per_share = float(price) * (stop_distance_pct / 100.0)
            if risk_per_share <= 0:
                return PositionSizingResult(
                    shares=0,
                    notional=0.0,
                    risk_amount=0.0,
                    risk_pct=0.0,
                    reject_reason="risk_per_share <= 0",
                )
            shares_by_risk = int(risk_budget / risk_per_share)

        port_sym_pct = effective_symbol_allocation_cap_pct(
            self._app_config,
            account_equity=float(account_equity),
            regime_score=regime_score,
            symbol_upper=symbol,
        )
        base_sym_pct = (
            float(port_sym_pct)
            if port_sym_pct > 0
            else float(self.max_exposure_per_symbol_pct)
        )
        if self._symbol_cap_floor_pp > 0.0:
            sym_pct = max(base_sym_pct, self._symbol_cap_floor_pp)
        else:
            sym_pct = base_sym_pct
        sym_u = str(symbol or "").strip().upper()
        if (
            sym_u in self.leader_override_symbols
            and self.leader_max_symbol_exposure_pct is not None
            and self.leader_max_symbol_exposure_pct > 0.0
        ):
            sym_pct = max(float(sym_pct), float(self.leader_max_symbol_exposure_pct))
        try:
            _crf = max(1.0, float(cap_relax_factor or 1.0))
        except (TypeError, ValueError):
            _crf = 1.0
        if _crf > 1.0:
            sym_pct = min(100.0, float(sym_pct) * _crf)
        pct_cap = account_equity * (sym_pct / 100.0)
        if self.max_position_dollar_cap is not None and self.max_position_dollar_cap > 0:
            max_notional = min(pct_cap, self.max_position_dollar_cap)
        else:
            max_notional = pct_cap
        current_sym = _symbol_row_notional(symbol, current_positions)
        remaining_usd = max(0.0, float(max_notional) - current_sym)
        shares_cap = int(remaining_usd / float(price)) if price > 0 else 0

        if (
            self.micro_cap_relief_enabled
            and shares_by_risk >= 1
            and remaining_usd > 1e-9
            and shares_cap < 1
        ):
            if self.micro_cap_relief_mode == "full_risk":
                shares_cap = int(shares_by_risk)
                trim_parts.append("micro_cap_relief_full_risk")
            else:
                shares_cap = max(shares_cap, min(1, int(shares_by_risk)))
                trim_parts.append("micro_cap_relief_one_share")

        shares = min(shares_by_risk, shares_cap)
        if shares < shares_by_risk:
            trim_parts.append("symbol_exposure_cap")
        if shares < 1 and self._small_fill_one_share_ok(remaining_usd, float(price)):
            shares = 1
            trim_parts.append("small_fill_minimum_1_share")
        if shares <= 0:
            return PositionSizingResult(
                shares=0,
                notional=0.0,
                risk_amount=0.0,
                risk_pct=0.0,
                reject_reason="symbol cap remaining yields zero shares",
            )

        confidence_applied: float | None = None
        _apply_confidence_sizing = self.confidence_sizing_enabled and not (
            self.confidence_suppress_when_proportional
            and self.signal_strength_mapping == "proportional"
        )
        if _apply_confidence_sizing:
            if ohlcv_df is None or getattr(ohlcv_df, "empty", True):
                return PositionSizingResult(
                    shares=0,
                    notional=0.0,
                    risk_amount=0.0,
                    risk_pct=0.0,
                    reject_reason="confidence_sizing enabled but ohlcv_df missing or empty",
                )
            conf_sum, _bd = confidence_score_trend_momentum_volume(
                ohlcv_df,
                ma_slow=self._conf_ma_slow,
                momentum_bars=self._conf_momentum_bars,
                volume_bars=self._conf_volume_bars,
            )
            confidence_applied = float(conf_sum)
            _pre_conf = shares
            shares = max(0, int(round(float(shares) * confidence_applied)))
            if shares < _pre_conf:
                trim_parts.append("confidence_sizing")
            if shares < 1 and self._small_fill_one_share_ok(remaining_usd, float(price)):
                shares = 1
                trim_parts.append("small_fill_minimum_1_share_after_confidence")
            if shares <= 0:
                return PositionSizingResult(
                    shares=0,
                    notional=0.0,
                    risk_amount=0.0,
                    risk_pct=0.0,
                    reject_reason="confidence_sizing scaled shares to zero",
                    confidence_score=confidence_applied,
                )

        notional = shares * float(price)
        risk_amount = shares * risk_per_share
        risk_pct = (risk_amount / account_equity) * 100.0

        apply_haircut = (
            (not use_atr_path)
            and self.high_vol_enabled
            and atr_pct is not None
            and atr_pct == atr_pct
            and float(atr_pct) > self.high_vol_atr_threshold
        )
        if apply_haircut:
            skip_haircut = (
                self.allow_high_volatility_min_regime_score is not None
                and regime_score is not None
                and int(regime_score) >= int(self.allow_high_volatility_min_regime_score)
            )
            if not skip_haircut:
                _prev_sh = shares
                shares = max(1, int(shares * self.high_vol_size_multiplier))
                if shares < _prev_sh:
                    trim_parts.append("high_vol_reduction")
                notional = shares * float(price)
                risk_amount = shares * risk_per_share
                risk_pct = (risk_amount / account_equity) * 100.0

        if regime_size_multiplier is not None and regime_size_multiplier > 0:
            _prev_sh = shares
            shares = max(1, int(shares * float(regime_size_multiplier)))
            if shares < _prev_sh:
                trim_parts.append("regime_size_multiplier")
            notional = shares * float(price)
            risk_amount = shares * risk_per_share
            risk_pct = (risk_amount / account_equity) * 100.0

        _prev_dyn = shares
        shares = self._apply_dynamic_multipliers(
            shares,
            current_drawdown_pct=current_drawdown_pct,
            conviction=conviction,
            strategy_winrate=strategy_winrate,
        )
        if shares < _prev_dyn:
            trim_parts.append("dynamic_sizing")

        shares, _did_pct_bump = self._bump_shares_for_min_target_position_pct(
            shares, shares_cap, float(account_equity), float(price)
        )
        if _did_pct_bump:
            trim_parts.append("min_target_position_pct")
            notional = shares * float(price)
            risk_amount = shares * risk_per_share
            risk_pct = (risk_amount / account_equity) * 100.0 if account_equity > 0 else 0.0

        proposed_notional = shares * float(price)
        _theme_k = (
            str(theme_key).strip()
            if theme_key is not None and str(theme_key).strip() != ""
            else THEME_MAP.get(str(symbol).strip().upper(), str(symbol).strip().upper())
        )
        allowed_notional, budget_trimmed, budget_trim_reason = self._cap_notional_by_portfolio_budget(
            account_equity=account_equity,
            proposed_notional=proposed_notional,
            current_gross_exposure_pct=current_gross_exposure_pct,
            current_net_exposure_pct=current_net_exposure_pct,
            current_theme_exposure_pct=current_theme_exposure_pct,
            is_etf=is_etf,
            is_inverse_etf=is_inverse_etf,
            theme_key=_theme_k,
            regime_condition=regime_condition,
            regime_score=regime_score,
        )
        if budget_trimmed:
            trim_parts.append(budget_trim_reason or "portfolio_budget_trim")

        px = float(price)
        shares = int(allowed_notional / px) if px > 0 else 0
        if shares < 1 and self._small_fill_one_share_ok(allowed_notional, px):
            shares = 1
            trim_parts.append("small_fill_minimum_1_share_portfolio_budget")
        if shares <= 0:
            return PositionSizingResult(
                shares=0,
                notional=0.0,
                risk_amount=0.0,
                risk_pct=0.0,
                reject_reason="portfolio caps leave no room",
            )

        notional = shares * px
        risk_amount = shares * risk_per_share
        risk_pct = (risk_amount / account_equity) * 100.0 if account_equity > 0 else 0.0

        sector, sector_explicit = resolve_sector_key_for_sizing(
            symbol, symbol_sector, default_sector=self._sector_default
        )
        apply_sector_cap = bool(self._sector_enforce_caps_on_unknown) or bool(sector_explicit)
        if apply_sector_cap:
            current_sector_pct = float(sector_exposure_pct.get(sector, 0.0))
            _sec_allowed = float(self.max_exposure_per_sector_pct)
            if _crf > 1.0:
                _sec_allowed = min(100.0, _sec_allowed * _crf)
            max_new_sector_pct = max(0.0, _sec_allowed - current_sector_pct)
            if max_new_sector_pct <= 0.0:
                return PositionSizingResult(
                    shares=0,
                    notional=0.0,
                    risk_amount=0.0,
                    risk_pct=0.0,
                    reject_reason=f"sector {sector} at or above {self.max_exposure_per_sector_pct}% cap",
                )

            max_sector_notional = account_equity * (max_new_sector_pct / 100.0)
            shares_sector_cap = int(max_sector_notional / px) if px > 0 else 0
            if shares > shares_sector_cap:
                shares = shares_sector_cap
                trim_parts.append("sector_exposure_cap")
                notional = shares * px
                risk_amount = shares * risk_per_share
                risk_pct = (risk_amount / account_equity) * 100.0 if account_equity > 0 else 0.0

            if shares <= 0:
                return PositionSizingResult(
                    shares=0,
                    notional=0.0,
                    risk_amount=0.0,
                    risk_pct=0.0,
                    reject_reason="sector cap leaves no room for additional shares",
                )

        exposure_pct = (notional / account_equity) * 100.0 if account_equity > 0 else 0.0
        trimmed = len(trim_parts) > 0
        trim_reason = "; ".join(trim_parts) if trimmed else None

        return PositionSizingResult(
            shares=shares,
            notional=notional,
            risk_amount=risk_amount,
            risk_pct=risk_pct,
            exposure_pct=exposure_pct,
            trimmed=trimmed,
            trim_reason=trim_reason,
            confidence_score=confidence_applied,
        )

    def total_open_risk_pct(
        self,
        account_equity: float,
        positions_with_stops: list[tuple[float, float]],
    ) -> float:
        """Sum of (position_value * stop_distance_pct) / equity as pct."""
        if account_equity <= 0:
            return 0.0
        total_risk = sum(
            (notional * (stop_pct / 100.0)) / account_equity * 100.0
            for notional, stop_pct in positions_with_stops
        )
        return total_risk

    def would_exceed_max_open_risk(
        self,
        current_open_risk_pct: float,
        new_trade_risk_pct: float,
    ) -> bool:
        """True if existing open risk % plus this trade's risk % exceeds the configured ceiling."""
        return (float(current_open_risk_pct) + float(new_trade_risk_pct)) > self.max_open_risk_pct
