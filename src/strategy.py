"""
Entry/exit rules: mechanical strategy with defined exits before entries.

Default: Trend-following (price above 200D MA, pullback to 20D MA, volatility filter).
Exits: stop-loss (hard), take-profit (optional), time-based, kill-switch (full or partial trim).
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

import pandas as pd
import numpy as np

from .candlestick import detect_any as candlestick_detect_any
from .smart_exit import effective_smart_trail_pct


class StrategyType(Enum):
    TREND_FOLLOWING = "trend_following"
    MEAN_REVERSION = "mean_reversion"
    BREAKOUT = "breakout"


class ExitReason(Enum):
    STOP_LOSS = "stop_loss"
    TAKE_PROFIT = "tp"
    PARTIAL_TAKE_PROFIT = "partial_take_profit"
    TRAILING_STOP = "trail"
    TIME_BARS = "time_bars"
    KILL_SWITCH = "kill_switch"
    KILL_SWITCH_PARTIAL = "kill_switch_partial"
    SIGNAL_EXIT = "signal_exit"
    NEWS_SENTIMENT = "news_sentiment"  # negative sentiment + weak trend (rule engine)
    OPTION_STOP_LOSS = "option_stop_loss"
    OPTION_PROFIT_TAKE = "option_profit_take"
    OPTION_PROFIT_TAKE_PARTIAL = "option_profit_take_partial"
    OPTION_MAX_HOLD = "option_max_hold_days"
    OPTION_UNDERLYING_BREAK = "option_underlying_break_signal"
    OPTION_SPREAD_TOO_WIDE = "option_spread_too_wide"
    OPTION_PNL_TRAIL = "option_pnl_trail"
    OPTION_END_OF_DAY = "option_end_of_day"
    RISK_CAP_REBALANCE = "risk_cap_rebalance"
    OVERWEIGHT_TRIM = "overweight_trim"
    MOMENTUM_FADE = "momentum_fade"


@dataclass
class EntrySignal:
    symbol: str
    side: str  # "long" | "short"
    strength: float
    stop_pct: float
    take_profit_pct: float | None
    time_bars_exit: int
    metadata: dict[str, Any]
    allowed: bool = True  # for :func:`~src.trading_engine.score_conviction` (+40 when True)


@dataclass
class ExitSignal:
    symbol: str
    reason: ExitReason
    metadata: dict[str, Any]


def trend_long_scan_ma_filter_ok(
    *,
    close: float,
    ma_fast: float | None,
    ma_slow: float | None,
    trend_filter_cfg: dict[str, Any] | None,
    long_require_ma_stack: bool,
) -> bool:
    """
    Live-loop prefilter: require close above fast MA; optionally above slow MA.

    ``strategy.trend_filter``:
      - ``require_above_both_ma`` (default True): close must be above fast and slow MA.
      - If False and ``allow_above_fast_ma`` is True: only above fast MA is required (slow MA skipped).
      - If False and ``allow_above_fast_ma`` is False: slow MA is still required (same as both for practical purposes here).
      - ``ema_tolerance_pct`` — percent points (``0.1`` → 0.1%%): allow close slightly below each required MA,
        down to ``MA × (1 − pct/100)`` (same units as ``trend_following.pullback_tolerance_pct``).

    ``long_require_ma_stack``: when True, fail if fast MA <= slow MA (regime entry policy).
    """
    tf = trend_filter_cfg or {}
    require_both = bool(tf.get("require_above_both_ma", True))
    allow_fast_only = bool(tf.get("allow_above_fast_ma", False))
    try:
        _tol_pct = float(tf.get("ema_tolerance_pct", 0) or 0)
    except (TypeError, ValueError):
        _tol_pct = 0.0
    _tol_pct = max(0.0, _tol_pct)
    _tol_frac = min(0.5, _tol_pct / 100.0)  # cap 50%% slack

    def _close_above_ma(cl: float, ma: float) -> bool:
        """Strict ``cl > ma`` when tolerance is 0; else allow down to ``ma * (1 - tol_frac)``."""
        try:
            m = float(ma)
        except (TypeError, ValueError):
            return False
        if m != m or m <= 0:
            return False
        thr = m * (1.0 - _tol_frac)
        return cl > thr

    def _bad_ma(x: float | None) -> bool:
        if x is None:
            return True
        try:
            v = float(x)
        except (TypeError, ValueError):
            return True
        return v != v or v <= 0

    if _bad_ma(ma_fast) or not _close_above_ma(close, float(ma_fast)):
        return False

    need_slow = require_both or not allow_fast_only
    if need_slow:
        if _bad_ma(ma_slow) or not _close_above_ma(close, float(ma_slow)):
            return False

    if long_require_ma_stack and not _bad_ma(ma_fast) and not _bad_ma(ma_slow):
        if float(ma_fast) <= float(ma_slow):
            return False
    return True


def target_position_pct_to_fraction(raw: Any) -> float:
    """
    ``strategy.exits.target_position_pct``: values ``> 1`` are percent-of-equity points (``8`` → ``0.08``);
    otherwise treated as a fraction in ``(0, 1]`` (``0.08`` → ``0.08``).
    """
    if raw is None or str(raw).strip() == "":
        return 0.0
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return 0.0
    if v <= 0.0:
        return 0.0
    if v > 1.0:
        return max(0.0, min(1.0, v / 100.0))
    return max(0.0, min(1.0, v))


def compute_overweight_trim_shares(
    *,
    equity: float,
    position_market_value_usd: float,
    qty: int,
    mid_price: float,
    target_fraction: float,
    aggressiveness: float,
) -> int:
    """
    Whole shares to sell when MV is above *equity × target_fraction*, selling only a fraction
    of the dollar excess per pass (``aggressiveness`` in ``(0, 1]``).

    Returns ``0`` when there is no overweight, inputs are invalid, or ``aggressiveness`` is ``0``.
    """
    eq = float(equity)
    if eq <= 0.0 or qty <= 0:
        return 0
    tf = max(0.0, min(1.0, float(target_fraction)))
    if tf <= 0.0:
        return 0
    px = float(mid_price)
    if px <= 0.0:
        return 0
    ag = max(0.0, min(1.0, float(aggressiveness)))
    if ag <= 0.0:
        return 0
    mv = float(position_market_value_usd)
    if mv <= 0.0:
        return 0
    target_mv = eq * tf
    if mv <= target_mv + 1e-9:
        return 0
    excess_mv = mv - target_mv
    trim_mv = excess_mv * ag
    raw_sh = int(trim_mv / px)
    if raw_sh <= 0:
        return 0
    return min(raw_sh, int(qty))


def _atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int) -> pd.Series:
    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low - close.shift(1)).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()


class PlayerFocus:
    """Strategy style: institutional (follow big volume), retail (faster/short horizon), neutral."""
    NEUTRAL = "neutral"
    INSTITUTIONAL = "institutional"
    RETAIL = "retail"


class TrendFollowingStrategy:
    """
    Trend-following entries from config strategy.trend_following + strategy.retail (when player_focus=retail).
    Default yaml: retail + entry_mode=momentum → close > slow MA and > fast MA (e.g. 50/10), ATR% cap, optional candlestick filter.
    entry_mode=pullback → close > slow MA and near fast MA within tolerance.
    Exits: stop, partial + trailing, time, kill-switch (see strategy.exits); retail uses retail.time_bars_exit for time exit.
    Live loop passes "bars_held" as calendar days since entry — align time_bars_exit with that semantics for Alpaca loop.
    """

    def __init__(self, config: dict[str, Any]):
        strat = config.get("strategy", {})
        tf = strat.get("trend_following", {})
        exits = strat.get("exits", {})

        self.player_focus = (strat.get("player_focus") or PlayerFocus.NEUTRAL).strip().lower()
        inst = strat.get("institutional", {})
        self.institutional_min_volume_ratio = float(inst.get("min_volume_ratio_vs_avg", 1.2))
        retail = strat.get("retail", {})
        retail_ma_fast = int(retail.get("ma_fast", 10))
        retail_ma_slow = int(retail.get("ma_slow", 50))
        retail_time_bars = int(retail.get("time_bars_exit", 10))

        self.ma_fast = int(tf.get("ma_fast", 20))
        self.ma_slow = int(tf.get("ma_slow", 200))
        if self.player_focus == PlayerFocus.RETAIL:
            self.ma_fast = retail_ma_fast
            self.ma_slow = retail_ma_slow
        self.entry_mode = (tf.get("entry_mode") or "momentum").strip().lower()
        self.pullback_touch_ma_fast = bool(tf.get("pullback_touch_ma_fast", True))
        self.pullback_tolerance_pct = float(tf.get("pullback_tolerance_pct", 0.5))  # default 0.5%
        self.atr_period = int(tf.get("volatility_filter_atr_period", 14))
        self.max_atr_pct_for_entry = float(tf.get("max_atr_pct_for_entry", 2.0))
        self.bullish_regime_min_score_for_atr = int(tf.get("bullish_regime_min_score_for_atr", 4))
        _atr_bull = tf.get("max_atr_pct_for_entry_bullish")
        if _atr_bull is not None and str(_atr_bull).strip() != "":
            self.max_atr_pct_for_entry_bullish = float(_atr_bull)
        else:
            self.max_atr_pct_for_entry_bullish = self.max_atr_pct_for_entry * 1.25

        self.stop_loss_pct = float(exits.get("stop_loss_pct", 1.0))
        self.cooldown_after_stop_minutes = float(exits.get("cooldown_after_stop_minutes", 30))
        self.require_new_breakout_after_stop = bool(exits.get("require_new_breakout_after_stop", False))
        self.cooldown_after_profit_minutes = float(exits.get("cooldown_after_profit_minutes", 10))
        _cpp = exits.get("cooldown_after_profit_partial_minutes")
        self.cooldown_after_profit_partial_minutes = (
            float(_cpp) if _cpp is not None and str(_cpp).strip() != "" else 2.0
        )
        self.strong_trend_reconfirm_bypass_cooldown = bool(
            exits.get("strong_trend_reconfirm_bypass_cooldown", False)
        )
        _srm = exits.get("strong_trend_reconfirm_min_regime_score")
        if _srm is not None and str(_srm).strip() != "":
            self.strong_trend_reconfirm_min_regime_score = int(_srm)
        else:
            self.strong_trend_reconfirm_min_regime_score = None
        self.require_price_above_exit_after_profit = bool(exits.get("require_price_above_exit_after_profit", True))
        # When True and daily structure passes :meth:`_strong_trend_structure_reconfirm_ok`, skip the
        # ``require_price_above_exit_after_profit`` close-vs-exit-price gate (cooldown bypass unchanged).
        self.strong_momentum_bypass_profit_reentry_price = bool(
            exits.get("strong_momentum_bypass_profit_reentry_price", False)
        )
        tp = exits.get("take_profit_pct")
        self.take_profit_pct = float(tp) if tp is not None and tp != "" else None
        _tgp = exits.get("trigger_profit_pct")
        _ptp = exits.get("partial_take_profit_pct", 2.0)
        if _tgp is not None and str(_tgp).strip() != "":
            self.partial_take_profit_pct = float(_tgp)
        elif _ptp is not None and str(_ptp).strip() != "":
            self.partial_take_profit_pct = float(_ptp)
        else:
            self.partial_take_profit_pct = 2.0
        self.partial_exit_ratio = float(exits.get("partial_exit_ratio", 0.5))
        if bool(exits.get("trim_winners_enabled", False)):
            _tt = exits.get("trim_threshold_pct")
            if _tt is not None and str(_tt).strip() != "":
                self.partial_take_profit_pct = float(_tt)
            _tf = exits.get("trim_fraction")
            if _tf is not None and str(_tf).strip() != "":
                self.partial_exit_ratio = float(_tf)
        # Meaningful-profit partial arm (%% vs entry); overrides trim_winner threshold when set.
        _ptrim_raw = exits.get("partial_trim_trigger_pct")
        self.partial_trim_trigger_pct = None
        if _ptrim_raw is not None and str(_ptrim_raw).strip() != "":
            try:
                _ptf = float(_ptrim_raw)
            except (TypeError, ValueError):
                pass
            else:
                if _ptf > 0:
                    self.partial_trim_trigger_pct = _ptf
                    self.partial_take_profit_pct = _ptf
        self.trim_on_overweight = bool(exits.get("trim_on_overweight", False))
        self.overweight_target_fraction = target_position_pct_to_fraction(exits.get("target_position_pct"))
        _tagg = exits.get("trim_aggressiveness", 1.0)
        try:
            self.trim_aggressiveness = max(0.0, min(1.0, float(_tagg)))
        except (TypeError, ValueError):
            self.trim_aggressiveness = 1.0
        self.use_trailing_stop = bool(exits.get("use_trailing_stop", True))
        self.trailing_stop_pct = float(exits.get("trailing_stop_pct", 1.0))
        self.time_bars_exit = int(exits.get("time_bars_exit", 10))
        if self.player_focus == PlayerFocus.RETAIL:
            self.time_bars_exit = retail_time_bars
        ks = exits.get("kill_switch", {})
        ks = ks if isinstance(ks, dict) else {}
        self.kill_switch_max_spread_pct = float(ks.get("max_spread_pct", 0.25))
        # Kill-switch uses ATR% = (ATR/close)*100; config in percent (e.g. 3.0 = 3%)
        _atr_raw = ks.get("max_atr_pct") or ks.get("max_atr_multiple")
        if _atr_raw is None or str(_atr_raw).strip() == "":
            _atr_raw = ks.get("drawdown_threshold_pct")
        self.kill_switch_max_atr_pct = float(_atr_raw if _atr_raw is not None and str(_atr_raw).strip() != "" else 3.0)
        _ks_mode = str(ks.get("mode") or "full").strip().lower()
        self.kill_switch_mode = "partial" if _ks_mode == "partial" else "full"
        try:
            _ksf_raw = ks.get("sell_fraction")
            if _ksf_raw is None or str(_ksf_raw).strip() == "":
                _ksf_raw = ks.get("partial_sell_pct")
            _ksf = float(_ksf_raw if _ksf_raw is not None and str(_ksf_raw).strip() != "" else 0.5)
        except (TypeError, ValueError):
            _ksf = 0.5
        self.kill_switch_sell_fraction = max(0.0, min(1.0, _ksf))
        st = exits.get("smart_trailing")
        st = st if isinstance(st, dict) else {}
        self.smart_trailing_enabled = bool(st.get("enabled", False))
        self.smart_trailing_activate_profit_pct = float(st.get("activate_profit_pct", 3.0) or 0.0)
        self.smart_trailing_trail_pct = float(st.get("trail_pct", 2.0) or 0.0)
        self.smart_trailing_scale_out: list[tuple[float, float]] = []
        _raw_sc = st.get("scale_out") or []
        if isinstance(_raw_sc, list):
            for row in _raw_sc:
                if not isinstance(row, dict):
                    continue
                try:
                    _pp = float(row.get("profit_pct", 0) or 0)
                    _sp = float(row.get("sell_pct", 0) or 0)
                except (TypeError, ValueError):
                    continue
                if _pp > 0 and _sp > 0:
                    self.smart_trailing_scale_out.append((_pp, _sp))
        self.smart_trailing_scale_out.sort(key=lambda x: x[0])
        _smh = st.get("min_hold_minutes")
        self.smart_trailing_min_hold_minutes = (
            float(_smh) if _smh is not None and str(_smh).strip() != "" else 0.0
        )
        _dtp = st.get("dynamic_trail_pct")
        _dtp = _dtp if isinstance(_dtp, dict) else {}
        self.smart_trailing_dynamic_trail_enabled = bool(_dtp.get("enabled", False))
        self.smart_trailing_dynamic_trail_floor_pct = float(_dtp.get("floor_pct", 1.5) or 1.5)
        self.smart_trailing_dynamic_trail_atr_mult = float(_dtp.get("atr_mult", 1.2) or 1.2)
        # 0 = disabled. Blocks partial, trail, time, kill-switch (not stop-loss). Live: wall-clock minutes.
        self.min_hold_minutes = float(exits.get("min_hold_minutes", 0) or 0)
        # Live discretionary trims (risk-cap clip, overweight trim, partial unrealized-P/L trim) wait for min_hold.
        self.no_trim_before_min_hold = bool(exits.get("no_trim_before_min_hold", False))
        # Defer *trims* (not stops) until the daily MA trend filter no longer holds (see live exit pass).
        self.require_signal_break_for_trim = bool(exits.get("require_signal_break_for_trim", False))
        _dnw = exits.get("do_not_sell_winners_early")
        _dnw = _dnw if isinstance(_dnw, dict) else {}
        self.do_not_sell_winners_early_enabled = bool(_dnw.get("enabled", False))
        try:
            self.do_not_sell_winners_early_min_pnl_pct = float(
                _dnw.get("min_pnl_pct", 2.0) or 2.0
            )
        except (TypeError, ValueError):
            self.do_not_sell_winners_early_min_pnl_pct = 2.0
        _aslo = exits.get("avoid_stop_loss_first_minutes_after_open")
        self.avoid_stop_loss_first_minutes_after_open = (
            float(_aslo) if _aslo is not None and str(_aslo).strip() != "" else 0.0
        )

        _mf = exits.get("momentum_fade")
        _mf = _mf if isinstance(_mf, dict) else {}
        self.momentum_fade_enabled = bool(_mf.get("enabled", False))
        self.momentum_fade_close_below_fast_ma = bool(_mf.get("close_below_fast_ma", True))
        self.momentum_fade_min_profit_pct = float(_mf.get("min_profit_pct", 0.0) or 0.0)
        _mfr = _mf.get("rsi_confirm") if isinstance(_mf.get("rsi_confirm"), dict) else {}
        self.momentum_fade_rsi_confirm_enabled = bool(_mfr.get("enabled", False))
        try:
            self.momentum_fade_rsi_period = max(2, int(_mfr.get("period", 14) or 14))
        except (TypeError, ValueError):
            self.momentum_fade_rsi_period = 14
        try:
            self.momentum_fade_rsi_below = float(_mfr.get("below", 48.0) or 48.0)
        except (TypeError, ValueError):
            self.momentum_fade_rsi_below = 48.0

        _tbt = exits.get("time_based_trim")
        _tbt = _tbt if isinstance(_tbt, dict) else {}
        self.time_based_trim_enabled = bool(_tbt.get("enabled", False))
        try:
            self.time_based_trim_after_minutes = float(_tbt.get("after_minutes", 0) or 0)
        except (TypeError, ValueError):
            self.time_based_trim_after_minutes = 0.0
        try:
            self.time_based_trim_fraction = float(_tbt.get("trim_fraction", 0.15) or 0.15)
        except (TypeError, ValueError):
            self.time_based_trim_fraction = 0.15
        self.time_based_trim_fraction = max(0.01, min(0.99, self.time_based_trim_fraction))
        try:
            self.time_based_trim_min_profit_pct = float(_tbt.get("min_profit_pct", 0.0) or 0.0)
        except (TypeError, ValueError):
            self.time_based_trim_min_profit_pct = 0.0

        cf = strat.get("candlestick_filter", {})
        self.candlestick_enabled = bool(cf.get("enabled", False))
        self.candlestick_patterns = list(cf.get("patterns", []) or [])

        _raw_tf = strat.get("trend_filter")
        self.trend_filter_cfg: dict[str, Any] | None = (
            _raw_tf if isinstance(_raw_tf, dict) else None
        )

        _ess = tf.get("entry_signal_strength")
        _ess = _ess if isinstance(_ess, dict) else {}
        self.entry_signal_strength_enabled = bool(_ess.get("enabled", True))

        port = config.get("portfolio") or {}
        _srank = port.get("signal_ranking") if isinstance(port.get("signal_ranking"), dict) else {}
        _evt = _srank.get("event_triggers")
        self._portfolio_signal_ranking_event_triggers = _evt if isinstance(_evt, dict) else {}
        from .alpha_config import effective_composite_weights

        self._composite_weight_map: dict[str, float] = effective_composite_weights(config)

        ps_conf = (config.get("position_sizing") or {}).get("confidence_sizing") or {}
        try:
            self._composite_rank_momentum_bars = int(ps_conf.get("momentum_bars", 10))
        except (TypeError, ValueError):
            self._composite_rank_momentum_bars = 10
        try:
            self._composite_rank_volume_bars = int(ps_conf.get("volume_bars", 20))
        except (TypeError, ValueError):
            self._composite_rank_volume_bars = 20

        mr = config.get("market_regime") or {}
        _ahv = mr.get("allow_high_volatility_min_regime_score")
        if _ahv is not None and str(_ahv).strip() != "":
            self.allow_high_volatility_min_regime_score = int(_ahv)
        else:
            self.allow_high_volatility_min_regime_score = None

    def effective_max_atr_pct_for_entry(self, regime_score: int | None) -> float:
        """Wider ATR%% cap when regime >= ``market_regime.allow_high_volatility_min_regime_score`` if set, else ``bullish_regime_min_score_for_atr``."""
        thr = (
            self.allow_high_volatility_min_regime_score
            if self.allow_high_volatility_min_regime_score is not None
            else self.bullish_regime_min_score_for_atr
        )
        if regime_score is not None and int(regime_score) >= thr:
            return float(self.max_atr_pct_for_entry_bullish)
        return float(self.max_atr_pct_for_entry)

    def _effective_minutes_held(self, minutes_held: float | None, bars_held: int) -> float:
        """Live: use wall-clock minutes when provided. Daily backtest: approximate bars × 1440."""
        if minutes_held is not None:
            return float(minutes_held)
        if self.min_hold_minutes <= 0:
            return float("inf")
        return float(bars_held) * 1440.0

    def _within_min_hold(self, minutes_held: float | None, bars_held: int) -> bool:
        if self.min_hold_minutes <= 0:
            return False
        return self._effective_minutes_held(minutes_held, bars_held) < self.min_hold_minutes

    def trim_deferred_for_min_hold(self, *, minutes_held: float | None, bars_held: int = 0) -> bool:
        """When ``no_trim_before_min_hold`` is set, discretionary live trims wait for ``min_hold_minutes``."""
        if not self.no_trim_before_min_hold or self.min_hold_minutes <= 0:
            return False
        # Live: wall-clock minutes required; unknown entry time → do not block trims.
        if minutes_held is None:
            return False
        return self._within_min_hold(minutes_held, bars_held)

    def _kill_switch_long_exit_signal(
        self,
        symbol: str,
        *,
        spread_pct: float | None,
        atr_pct: float | None,
        current_qty: int,
    ) -> ExitSignal | None:
        """Spread/ATR kill-switch: full exit, or partial trim when ``kill_switch.mode: partial``."""
        over_spread = spread_pct is not None and float(spread_pct) > self.kill_switch_max_spread_pct
        over_atr = atr_pct is not None and float(atr_pct) > self.kill_switch_max_atr_pct
        if not over_spread and not over_atr:
            return None
        md: dict[str, Any] = {}
        if over_spread and spread_pct is not None:
            md["spread_pct"] = float(spread_pct)
        if over_atr and atr_pct is not None:
            md["atr_pct"] = float(atr_pct)
        qty = int(current_qty)
        if self.kill_switch_mode == "partial" and qty > 0:
            frac = float(self.kill_switch_sell_fraction)
            if frac <= 0.0:
                frac = 0.5
            qty_to_sell = max(1, int(qty * frac))
            qty_to_sell = min(qty_to_sell, qty)
            if qty_to_sell >= qty and qty > 1:
                qty_to_sell = qty - 1
            md["qty_to_sell"] = qty_to_sell
            return ExitSignal(symbol=symbol, reason=ExitReason.KILL_SWITCH_PARTIAL, metadata=md)
        return ExitSignal(symbol=symbol, reason=ExitReason.KILL_SWITCH, metadata=md)

    def _smart_trailing_deferred(self, minutes_held: float | None) -> bool:
        """True → skip smart scale-out and smart trail (``strategy.exits.smart_trailing.min_hold_minutes``)."""
        if not self.smart_trailing_enabled or self.smart_trailing_min_hold_minutes <= 0:
            return False
        if minutes_held is None:
            return False
        return float(minutes_held) < float(self.smart_trailing_min_hold_minutes)

    def smart_trailing_effective_trail_pct(self, atr_pct: float | None) -> float:
        """Trailing width in %% for smart trail (fixed YAML or ``dynamic_trail_pct`` vs ATR%%)."""
        return effective_smart_trail_pct(
            fixed_trail_pct=self.smart_trailing_trail_pct,
            dynamic_enabled=self.smart_trailing_dynamic_trail_enabled,
            floor_pct=self.smart_trailing_dynamic_trail_floor_pct,
            atr_mult=self.smart_trailing_dynamic_trail_atr_mult,
            atr_pct=atr_pct,
        )

    def atr_pct(self, df: pd.DataFrame) -> pd.Series:
        if df.empty or len(df) < self.atr_period:
            return pd.Series(dtype=float)
        atr = _atr(df["high"], df["low"], df["close"], self.atr_period)
        return (atr / df["close"]) * 100

    @staticmethod
    def _rsi_last(close: pd.Series, period: int) -> float | None:
        if len(close) < period + 1:
            return None
        delta = close.diff()
        gain = delta.clip(lower=0.0)
        loss = (-delta).clip(lower=0.0)
        ag = gain.rolling(period).mean()
        al = loss.rolling(period).mean()
        rs = ag / al.replace(0.0, np.nan)
        rsi = 100.0 - (100.0 / (1.0 + rs))
        v = rsi.iloc[-1]
        if pd.isna(v):
            return None
        out = float(v)
        if out != out:
            return None
        return out

    def evaluate_momentum_fade_exit(
        self,
        symbol: str,
        ohlcv_df: pd.DataFrame,
        entry_price: float,
        current_price: float,
        *,
        minutes_held: float | None = None,
    ) -> ExitSignal | None:
        """Exit long when price drops below the fast MA (optional RSI confirmation). Uses daily OHLCV."""
        _ = minutes_held
        if not self.momentum_fade_enabled or not self.momentum_fade_close_below_fast_ma:
            return None
        if ohlcv_df.empty or "close" not in ohlcv_df.columns:
            return None
        need = int(self.ma_fast) + 2
        if self.momentum_fade_rsi_confirm_enabled:
            need = max(need, int(self.momentum_fade_rsi_period) + 3)
        if len(ohlcv_df) < need:
            return None
        close = ohlcv_df["close"].astype(float)
        try:
            ep = float(entry_price)
            cp = float(current_price)
        except (TypeError, ValueError):
            return None
        if ep <= 0:
            return None
        ret_pct = (cp - ep) / ep * 100.0
        if ret_pct < float(self.momentum_fade_min_profit_pct):
            return None
        ma = close.rolling(int(self.ma_fast)).mean()
        ma_now = ma.iloc[-1]
        if pd.isna(ma_now) or float(ma_now) <= 0:
            return None
        ma_now = float(ma_now)
        c_last = float(close.iloc[-1])
        if c_last >= ma_now:
            return None
        if self.momentum_fade_rsi_confirm_enabled:
            rsi = self._rsi_last(close, int(self.momentum_fade_rsi_period))
            if rsi is None:
                return None
            if rsi >= float(self.momentum_fade_rsi_below):
                return None
        return ExitSignal(
            symbol=symbol,
            reason=ExitReason.MOMENTUM_FADE,
            metadata={
                "ret_pct": float(ret_pct),
                "ma_fast": ma_now,
                "close": c_last,
            },
        )

    def min_history_bars_for_entry(self, symbol: str) -> int:
        """SQQQ skips MA/pullback gates; only ATR + fast MA window needed."""
        if str(symbol or "").upper() == "SQQQ":
            return max(self.atr_period + 1, self.ma_fast, 20)
        return self.ma_slow

    def _strong_trend_structure_reconfirm_ok(
        self,
        symbol: str,
        df: pd.DataFrame | None,
        regime_score: int | None,
    ) -> bool:
        """Daily MA stack + optional min regime score (shared by cooldown bypass and profit re-entry bypass)."""
        if df is None or df.empty or len(df) < self.min_history_bars_for_entry(symbol):
            return False
        close = df["close"]
        ma_fast_s = close.rolling(self.ma_fast).mean()
        ma_slow_s = close.rolling(self.ma_slow).mean()
        price = float(close.iloc[-1])
        ma_f = ma_fast_s.iloc[-1]
        ma_s = ma_slow_s.iloc[-1]
        if not trend_long_scan_ma_filter_ok(
            close=price,
            ma_fast=float(ma_f) if not pd.isna(ma_f) else None,
            ma_slow=float(ma_s) if not pd.isna(ma_s) else None,
            trend_filter_cfg=self.trend_filter_cfg,
            long_require_ma_stack=False,
        ):
            return False
        if self.strong_trend_reconfirm_min_regime_score is not None:
            if regime_score is None or int(regime_score) < int(self.strong_trend_reconfirm_min_regime_score):
                return False
        return True

    def strong_trend_reconfirm_ok(
        self,
        symbol: str,
        df: pd.DataFrame | None,
        regime_score: int | None,
    ) -> bool:
        """When True, callers may bypass post-stop / post-profit cooldown timers (not breakout/price rules)."""
        if not self.strong_trend_reconfirm_bypass_cooldown:
            return False
        return self._strong_trend_structure_reconfirm_ok(symbol, df, regime_score)

    def long_trend_structure_still_strong(self, symbol: str, df: pd.DataFrame | None) -> bool:
        """
        True when the daily close still passes the same MA test as the entry trend prefilter
        (``trend_long_scan_ma_filter_ok`` with ``long_require_ma_stack`` false — same structure leg as
        :meth:`_strong_trend_structure_reconfirm_ok`).

        The live loop uses this with ``exits.require_signal_break_for_trim`` to avoid trimming into a
        still-intact trend (risk/weighting and P/L-based partials).
        """
        sym_u = str(symbol or "").upper()
        if sym_u == "SQQQ":
            return False
        if df is None or df.empty or len(df) < self.ma_slow or "close" not in df.columns:
            return False
        close = df["close"]
        ma_fast_s = close.rolling(self.ma_fast).mean()
        ma_slow_s = close.rolling(self.ma_slow).mean()
        price = float(close.iloc[-1])
        ma_f = ma_fast_s.iloc[-1]
        ma_s = ma_slow_s.iloc[-1]
        if pd.isna(ma_f) or pd.isna(ma_s):
            return False
        return trend_long_scan_ma_filter_ok(
            close=price,
            ma_fast=float(ma_f),
            ma_slow=float(ma_s),
            trend_filter_cfg=self.trend_filter_cfg,
            long_require_ma_stack=False,
        )

    def strong_momentum_reentry_ok(
        self,
        symbol: str,
        df: pd.DataFrame | None,
        regime_score: int | None,
    ) -> bool:
        """
        When True, callers may skip ``require_price_above_exit_after_profit`` (close vs last exit price).

        Uses the same MA stack + optional min regime score as :meth:`strong_trend_reconfirm_ok`, but does
        not require ``strong_trend_reconfirm_bypass_cooldown`` to be enabled.
        """
        if not self.strong_momentum_bypass_profit_reentry_price:
            return False
        return self._strong_trend_structure_reconfirm_ok(symbol, df, regime_score)

    def strong_momentum_structure_ok(
        self,
        symbol: str,
        df: pd.DataFrame | None,
        regime_score: int | None,
    ) -> bool:
        """
        True when daily MA stack (+ optional min regime score) reads as strong trend / momentum.

        Used by the live loop for ``portfolio.allow_add_on_strong_momentum`` (add-on path when ``allow_add``
        is false but tape is strong).
        """
        return self._strong_trend_structure_reconfirm_ok(symbol, df, regime_score)

    def generate_entry(
        self,
        symbol: str,
        df: pd.DataFrame,
        spread_pct: float | None = None,
        atr_pct_now: float | None = None,
        regime_score: int | None = None,
    ) -> EntrySignal | None:
        """Generate entry only when trend + pullback + volatility filter pass.
        atr_pct_now must be ATR% = (ATR/close)*100."""

        def _entry_debug(
            trend_ok: bool,
            pullback_ok: bool,
            momentum_ok: bool,
            vol_ok: bool,
        ) -> None:
            print(
                f"{symbol} entry debug → "
                f"trend={trend_ok}, "
                f"pullback={pullback_ok}, "
                f"momentum={momentum_ok}, "
                f"volatility={vol_ok}",
                flush=True,
            )

        sym_u = str(symbol or "").upper()
        skip_ma_check = sym_u == "SQQQ"
        skip_pullback_check = sym_u == "SQQQ"
        min_rows = self.min_history_bars_for_entry(symbol)
        if df is None or len(df) < min_rows:
            _entry_debug(False, False, False, False)
            return None

        close = df["close"]
        atr_pct = self.atr_pct(df)
        if atr_pct.empty or pd.isna(atr_pct.iloc[-1]):
            _entry_debug(False, False, False, False)
            return None

        atr_last = float(atr_pct.iloc[-1])
        _atr_cap = self.effective_max_atr_pct_for_entry(regime_score)
        vol_ok = atr_last <= _atr_cap
        if not vol_ok:
            _entry_debug(False, False, False, vol_ok)
            return None

        ma_fast = close.rolling(self.ma_fast).mean()
        ma_slow = close.rolling(self.ma_slow).mean()

        price = close.iloc[-1]
        ma_f = ma_fast.iloc[-1]
        ma_s = ma_slow.iloc[-1]

        momentum_ok = self.entry_mode == "momentum"

        if skip_ma_check and skip_pullback_check:
            trend_ok = True
            pullback_ok = True
        else:
            trend_ok = trend_long_scan_ma_filter_ok(
                close=float(price),
                ma_fast=float(ma_f) if not pd.isna(ma_f) else None,
                ma_slow=float(ma_s) if not pd.isna(ma_s) else None,
                trend_filter_cfg=self.trend_filter_cfg,
                long_require_ma_stack=False,
            )
            if not trend_ok:
                if self.entry_mode == "momentum":
                    pullback_ok = True
                else:
                    tol = self.pullback_tolerance_pct / 100.0
                    pullback_ok = (not self.pullback_touch_ma_fast) or (
                        not pd.isna(ma_f)
                        and float(ma_f) > 0
                        and abs(float(price) - float(ma_f)) / float(ma_f) <= tol
                    )
                _entry_debug(trend_ok, pullback_ok, momentum_ok, vol_ok)
                return None

            if self.entry_mode != "momentum":
                tol = self.pullback_tolerance_pct / 100.0
                near_ok = (not self.pullback_touch_ma_fast) or (
                    not pd.isna(ma_f)
                    and float(ma_f) > 0
                    and abs(float(price) - float(ma_f)) / float(ma_f) <= tol
                )
                pullback_ok = near_ok
                if not near_ok:
                    _entry_debug(trend_ok, False, momentum_ok, vol_ok)
                    return None
            else:
                pullback_ok = True

        # Kill-switch: don't enter if spread/volatility already bad (atr_pct_now is ATR%)
        if spread_pct is not None and spread_pct > self.kill_switch_max_spread_pct:
            _entry_debug(trend_ok, pullback_ok, momentum_ok, vol_ok)
            return None
        if atr_pct_now is not None and atr_pct_now > self.kill_switch_max_atr_pct:
            _entry_debug(trend_ok, pullback_ok, momentum_ok, vol_ok)
            return None

        # Institutional: only enter when volume is elevated (proxy for institutional activity)
        if self.player_focus == PlayerFocus.INSTITUTIONAL and "volume" in df.columns and len(df) >= 20:
            vol = df["volume"]
            avg_vol = vol.rolling(20).mean().iloc[-1]
            if avg_vol and avg_vol > 0:
                volume_ratio = vol.iloc[-1] / avg_vol
                if volume_ratio < self.institutional_min_volume_ratio:
                    _entry_debug(trend_ok, pullback_ok, momentum_ok, vol_ok)
                    return None

        # Candlestick filter: only enter when one of the configured patterns appears on the last bar(s)
        if self.candlestick_enabled and not candlestick_detect_any(df, self.candlestick_patterns):
            _entry_debug(trend_ok, pullback_ok, momentum_ok, vol_ok)
            return None

        _entry_debug(trend_ok, pullback_ok, momentum_ok, vol_ok)

        strength = 1.0
        meta: dict[str, Any] = {"ma_fast": ma_f, "ma_slow": ma_s, "atr_pct": atr_last}
        if self.entry_signal_strength_enabled:
            try:
                # Local import avoids circular dependency (signal_ranking imports ``_atr`` from strategy).
                from .signal_ranking import trend_long_composite_rank

                _max_atr = float(self.effective_max_atr_pct_for_entry(regime_score))
                _cr, _bd, _den = trend_long_composite_rank(
                    df,
                    atr_pct=float(atr_last),
                    max_atr_pct=_max_atr,
                    ma_fast=int(self.ma_fast),
                    ma_slow=int(self.ma_slow),
                    momentum_bars=int(self._composite_rank_momentum_bars),
                    volume_bars=int(self._composite_rank_volume_bars),
                    event_triggers=self._portfolio_signal_ranking_event_triggers,
                    atr_period=int(self.atr_period),
                    composite_weights=self._composite_weight_map,
                )
                _d = float(_den) if _den else 4.0
                strength = max(0.0, min(1.0, float(_cr) / max(_d, 1e-12)))
                meta["composite_breakdown"] = dict(_bd)
                meta["composite_score"] = float(_cr)
                meta["composite_denom"] = float(_den)
                meta["entry_signal_strength_source"] = "composite_rank"
            except Exception:
                strength = 1.0
                meta["entry_signal_strength_source"] = "composite_rank_error_fallback"

        return EntrySignal(
            symbol=symbol,
            side="long",
            strength=float(strength),
            stop_pct=self.stop_loss_pct,
            take_profit_pct=self.partial_take_profit_pct or self.take_profit_pct,
            time_bars_exit=self.time_bars_exit,
            metadata=meta,
        )

    def entry_eval_components_for_log(
        self,
        symbol: str,
        df: pd.DataFrame,
        spread_pct: float | None,
        atr_pct_now: float | None,
        regime_score: int | None = None,
    ) -> tuple[bool | None, bool | None, bool | None, bool | None]:
        """(trend, pullback, momentum, volatility_aggregates) for :func:`entry_eval_log.log_entry_eval`.

        Volatility aggregates ATR entry band, kill-switch ATR (``atr_pct_now``), institutional volume,
        and candlestick filter when enabled. Mirrors :meth:`generate_entry` branching.
        """
        sym_u = str(symbol or "").upper()
        skip_ma_check = sym_u == "SQQQ"
        skip_pullback_check = sym_u == "SQQQ"
        min_rows = self.min_history_bars_for_entry(symbol)
        momentum_ok = self.entry_mode == "momentum"
        if df is None or len(df) < min_rows:
            return (None, None, momentum_ok, None)
        atr_pct_s = self.atr_pct(df)
        if atr_pct_s.empty or pd.isna(atr_pct_s.iloc[-1]):
            return (None, None, momentum_ok, None)
        atr_last = float(atr_pct_s.iloc[-1])
        _atr_cap_e = self.effective_max_atr_pct_for_entry(regime_score)
        vol_ok = atr_last <= _atr_cap_e
        if not vol_ok:
            return (None, None, momentum_ok, False)

        close = df["close"]
        ma_fast = close.rolling(self.ma_fast).mean()
        ma_slow = close.rolling(self.ma_slow).mean()
        price = close.iloc[-1]
        ma_f = ma_fast.iloc[-1]
        ma_s = ma_slow.iloc[-1]

        if skip_ma_check and skip_pullback_check:
            trend_ok = True
            pullback_ok = True
        else:
            trend_ok = trend_long_scan_ma_filter_ok(
                close=float(price),
                ma_fast=float(ma_f) if not pd.isna(ma_f) else None,
                ma_slow=float(ma_s) if not pd.isna(ma_s) else None,
                trend_filter_cfg=self.trend_filter_cfg,
                long_require_ma_stack=False,
            )
            if not trend_ok:
                if self.entry_mode == "momentum":
                    pullback_ok = True
                else:
                    tol = self.pullback_tolerance_pct / 100.0
                    pullback_ok = (not self.pullback_touch_ma_fast) or (
                        not pd.isna(ma_f)
                        and float(ma_f) > 0
                        and abs(float(price) - float(ma_f)) / float(ma_f) <= tol
                    )
                return (False, pullback_ok, momentum_ok, True)

            if self.entry_mode != "momentum":
                tol = self.pullback_tolerance_pct / 100.0
                near_ok = (not self.pullback_touch_ma_fast) or (
                    not pd.isna(ma_f)
                    and float(ma_f) > 0
                    and abs(float(price) - float(ma_f)) / float(ma_f) <= tol
                )
                pullback_ok = near_ok
                if not near_ok:
                    return (True, False, momentum_ok, True)
            else:
                pullback_ok = True

        kill_atr_ok = atr_pct_now is None or float(atr_pct_now) <= self.kill_switch_max_atr_pct
        inst_ok = True
        if self.player_focus == PlayerFocus.INSTITUTIONAL and "volume" in df.columns and len(df) >= 20:
            vol = df["volume"]
            avg_vol = vol.rolling(20).mean().iloc[-1]
            if avg_vol and avg_vol > 0:
                volume_ratio = vol.iloc[-1] / avg_vol
                inst_ok = volume_ratio >= self.institutional_min_volume_ratio

        cs_ok = True
        if self.candlestick_enabled:
            cs_ok = candlestick_detect_any(df, self.candlestick_patterns)

        vol_agg = kill_atr_ok and inst_ok and cs_ok
        return (trend_ok, pullback_ok, momentum_ok, vol_agg)

    def check_exit(
        self,
        symbol: str,
        entry_price: float,
        current_price: float,
        bars_held: int,
        spread_pct: float | None = None,
        atr_pct: float | None = None,
        *,
        partial_taken: bool = False,
        trail_high: float | None = None,
        current_qty: int = 0,
        minutes_held: float | None = None,
        minutes_since_session_open_et: float | None = None,
        smart_scale_out_index: int = 0,
        builtin_smart_trailing: bool = True,
        ohlcv_df: pd.DataFrame | None = None,
    ) -> ExitSignal | None:
        """Check for stop, trailing on remainder, partial take-profit, time, or kill-switch.
        Exit tags: ``stop_loss``, ``trail``, ``partial_take_profit`` (short full TP: ``tp``).
        atr_pct must be ATR% = (ATR/close)*100.
        min_hold_minutes: defers partial, trail, time, kill-switch until hold elapsed (live: wall clock).
        Stop-loss may be deferred by ``avoid_stop_loss_first_minutes_after_open`` vs session open (ET)."""
        ret_pct = (current_price - entry_price) / entry_price * 100

        defer_stop_open = (
            self.avoid_stop_loss_first_minutes_after_open > 0
            and minutes_since_session_open_et is not None
            and float(minutes_since_session_open_et) < self.avoid_stop_loss_first_minutes_after_open
        )
        if not defer_stop_open and ret_pct <= -self.stop_loss_pct:
            return ExitSignal(symbol=symbol, reason=ExitReason.STOP_LOSS, metadata={"ret_pct": ret_pct})
        if self._within_min_hold(minutes_held, bars_held):
            return None

        if bars_held >= self.time_bars_exit:
            return ExitSignal(symbol=symbol, reason=ExitReason.TIME_BARS, metadata={"bars_held": bars_held})
        if ohlcv_df is not None:
            mf = self.evaluate_momentum_fade_exit(
                symbol,
                ohlcv_df,
                entry_price,
                current_price,
                minutes_held=minutes_held,
            )
            if mf is not None:
                return mf
        _ks = self._kill_switch_long_exit_signal(
            symbol,
            spread_pct=spread_pct,
            atr_pct=atr_pct,
            current_qty=current_qty,
        )
        if _ks is not None:
            return _ks

        if self.smart_trailing_enabled:
            if builtin_smart_trailing:
                _st_def = self._smart_trailing_deferred(minutes_held)
                if not _st_def:
                    if (
                        self.smart_trailing_scale_out
                        and smart_scale_out_index < len(self.smart_trailing_scale_out)
                        and current_qty > 0
                    ):
                        _pp, _sp = self.smart_trailing_scale_out[smart_scale_out_index]
                        if ret_pct >= _pp:
                            qty_to_sell = max(1, int(current_qty * float(_sp)))
                            qty_to_sell = min(qty_to_sell, current_qty)
                            if qty_to_sell >= current_qty and current_qty > 1:
                                qty_to_sell = current_qty - 1
                            return ExitSignal(
                                symbol=symbol,
                                reason=ExitReason.PARTIAL_TAKE_PROFIT,
                                metadata={
                                    "ret_pct": ret_pct,
                                    "qty_to_sell": qty_to_sell,
                                    "smart_scale_level": smart_scale_out_index,
                                },
                            )
                    _eff_trail_pct = self.smart_trailing_effective_trail_pct(atr_pct)
                    if (
                        current_qty > 0
                        and ret_pct >= self.smart_trailing_activate_profit_pct
                        and _eff_trail_pct > 0
                    ):
                        eff_high = max(float(entry_price), float(current_price))
                        if trail_high is not None:
                            eff_high = max(eff_high, float(trail_high))
                        threshold = eff_high * (1.0 - _eff_trail_pct / 100.0)
                        if current_price <= threshold:
                            return ExitSignal(
                                symbol=symbol,
                                reason=ExitReason.TRAILING_STOP,
                                metadata={
                                    "ret_pct": ret_pct,
                                    "trail_high": eff_high,
                                    "smart_trailing": True,
                                    "trail_pct": _eff_trail_pct,
                                },
                            )
            # Full take-profit only here; staged trims stay on smart scale / trail (no low partial).
            if self.take_profit_pct is not None and current_qty > 0 and ret_pct >= float(self.take_profit_pct):
                return ExitSignal(symbol=symbol, reason=ExitReason.TAKE_PROFIT, metadata={"ret_pct": ret_pct})
            return None

        # Priority: trailing (post-partial) before partial take-profit (mutually exclusive in practice).
        if self.use_trailing_stop and partial_taken and current_qty > 0 and trail_high is not None:
            high = max(trail_high, current_price)
            threshold = high * (1 - self.trailing_stop_pct / 100.0)
            if current_price <= threshold:
                return ExitSignal(
                    symbol=symbol,
                    reason=ExitReason.TRAILING_STOP,
                    metadata={"ret_pct": ret_pct, "trail_high": high},
                )
        if self.take_profit_pct is not None and current_qty > 0 and ret_pct >= float(self.take_profit_pct):
            return ExitSignal(symbol=symbol, reason=ExitReason.TAKE_PROFIT, metadata={"ret_pct": ret_pct})
        if not partial_taken and current_qty > 0 and ret_pct >= self.partial_take_profit_pct:
            if self.take_profit_pct is None or ret_pct < float(self.take_profit_pct):
                qty_to_sell = max(1, int(current_qty * self.partial_exit_ratio))
                if qty_to_sell < current_qty:
                    return ExitSignal(
                        symbol=symbol,
                        reason=ExitReason.PARTIAL_TAKE_PROFIT,
                        metadata={"ret_pct": ret_pct, "qty_to_sell": qty_to_sell},
                    )
        return None

    def exit_eval_flags_for_log(
        self,
        symbol: str,
        entry_price: float,
        current_price: float,
        bars_held: int,
        *,
        partial_taken: bool = False,
        current_qty: int = 0,
        minutes_held: float | None = None,
        minutes_since_session_open_et: float | None = None,
    ) -> tuple[bool, bool, bool]:
        """(tp_hit, sl_hit, time_exit) for EXIT_EVAL logging; aligns with :meth:`check_exit` gating (min-hold, defer)."""
        _ = symbol
        ret_pct = (current_price - entry_price) / entry_price * 100
        defer_stop_open = (
            self.avoid_stop_loss_first_minutes_after_open > 0
            and minutes_since_session_open_et is not None
            and float(minutes_since_session_open_et) < self.avoid_stop_loss_first_minutes_after_open
        )
        sl_hit = (not defer_stop_open) and (ret_pct <= -self.stop_loss_pct)
        in_min_hold = self._within_min_hold(minutes_held, bars_held)
        time_exit = (not in_min_hold) and (bars_held >= self.time_bars_exit)
        tp_hit = (not in_min_hold) and current_qty > 0 and (
            (self.take_profit_pct is not None and ret_pct >= float(self.take_profit_pct))
            or (
                (not partial_taken)
                and ret_pct >= self.partial_take_profit_pct
                and (self.take_profit_pct is None or ret_pct < float(self.take_profit_pct))
            )
        )
        return (tp_hit, sl_hit, time_exit)

    def check_exit_short(
        self,
        symbol: str,
        entry_price: float,
        current_price: float,
        bars_held: int,
        stop_pct: float,
        take_profit_pct: float,
        time_bars_exit: int,
        spread_pct: float | None = None,
        atr_pct: float | None = None,
        *,
        minutes_held: float | None = None,
    ) -> ExitSignal | None:
        """Exit rules for short: stop when price rises, take profit when price falls. atr_pct for kill-switch."""
        ret_pct = (entry_price - current_price) / entry_price * 100  # profit when price falls
        if ret_pct <= -stop_pct:
            return ExitSignal(symbol=symbol, reason=ExitReason.STOP_LOSS, metadata={"ret_pct": ret_pct})
        if self._within_min_hold(minutes_held, bars_held):
            return None
        if bars_held >= time_bars_exit:
            return ExitSignal(symbol=symbol, reason=ExitReason.TIME_BARS, metadata={"bars_held": bars_held})
        if spread_pct is not None and spread_pct > self.kill_switch_max_spread_pct:
            return ExitSignal(symbol=symbol, reason=ExitReason.KILL_SWITCH, metadata={"spread_pct": spread_pct})
        if atr_pct is not None and atr_pct > self.kill_switch_max_atr_pct:
            return ExitSignal(symbol=symbol, reason=ExitReason.KILL_SWITCH, metadata={"atr_pct": atr_pct})
        if ret_pct >= take_profit_pct:
            return ExitSignal(symbol=symbol, reason=ExitReason.TAKE_PROFIT, metadata={"ret_pct": ret_pct})
        return None

    def exit_eval_flags_for_log_short(
        self,
        symbol: str,
        entry_price: float,
        current_price: float,
        bars_held: int,
        stop_pct: float,
        take_profit_pct: float,
        time_bars_exit: int,
        *,
        minutes_held: float | None = None,
    ) -> tuple[bool, bool, bool]:
        """(tp_hit, sl_hit, time_exit) for short exits; mirrors :meth:`check_exit_short` min-hold behavior."""
        _ = symbol
        ret_pct = (entry_price - current_price) / entry_price * 100
        sl_hit = ret_pct <= -stop_pct
        in_min_hold = self._within_min_hold(minutes_held, bars_held)
        time_exit = (not in_min_hold) and (bars_held >= time_bars_exit)
        tp_hit = (not in_min_hold) and (ret_pct >= take_profit_pct)
        return (tp_hit, sl_hit, time_exit)
