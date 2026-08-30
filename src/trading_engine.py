"""
Trading engine: orchestrates universe, strategy, sizing, portfolio risk, execution, compliance.

**Layer 1 (gross exposure):** after a valid long :class:`~src.strategy.EntrySignal`, the engine
enforces (1) current book vs :func:`~src.exposure_gates.block_new_entries_total_exposure`,
(2) :func:`~src.exposure_gates.hard_exposure_reject_if_projected_gross_exceeds_cap` (HARD: no relief;
``projected > 1.0`` or ``> min(1, cap)`` of equity), then
(3) :func:`~src.exposure_gates.layer1_reject_buy_if_projected_gross_exceeds_max` (relief / relax) *before*
max open-risk and order build. Optional ``portfolio.exposure_gates.soft_cap`` trims buy size toward the cap
instead of rejecting when projected gross would exceed the ceiling (portfolio budget rejects can retry at
remaining headroom).

Runs the full gate sequence before any trade and applies all rules.
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime, date
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger(__name__)

from .config_loader import load_config
from .universe import (
    MarketCalendar,
    UniverseFilter,
    MarketQualityGate,
    MarketQualityResult,
    last_bar_volume_from_ohlcv,
)
from .strategy import TrendFollowingStrategy, EntrySignal, ExitSignal
from .position_sizing import (
    PositionSizer,
    PositionSizingResult,
    clamp_position_sizing_to_max_buy_usd,
    scale_position_sizing_by_multiplier,
)
from .portfolio_risk import PortfolioRiskManager, PortfolioRiskState
from .execution import (
    ExecutionManager,
    ExecutionState,
    minutes_since_last_broker_structured_exit,
    parse_per_symbol_sell_cooldown_min,
    symbol_post_exit_cooldown_minutes,
)
from .compliance import ComplianceManager, PDTState
from .trade_filters import MacroEventBlackout, EarningsBlackout, VolatilityDoNotTrade
from .exposure import ETF_SYMBOLS, INVERSE_ETFS, THEME_MAP, compute_exposures
from .sector_config import parse_sector_config
from .adaptive import adaptive_effective_max_total_exposure
from .entry_filters import check_listed_defensive_tape_gates, check_trend_regime_filters
from .exposure_gates import (
    block_new_entries_total_exposure,
    entry_size_multiplier_tech_sector_over_cap,
    hard_exposure_reject_if_projected_gross_exceeds_cap,
    is_reduce_only_overexposed,
    layer1_reject_buy_if_projected_gross_exceeds_max,
    parse_portfolio_exposure_gates,
    parse_strong_signal_cap_relief,
    portfolio_hard_gross_blocks_new_entry,
    soft_cap_max_buy_usd,
)
from .profit_reentry_gate import entries_reentry_pullback_cfg, profit_reentry_price_allowed
from .risk_limits import resolve_reduce_only_gross_frac
from .decision_report import build_buy_decision_report


def _positions_rows_for_exposure(current_positions: dict[str, Any]) -> list[dict[str, Any]]:
    """Map ``current_positions`` values (``notional`` / ``market_value``) to rows for :func:`~src.exposure.compute_exposures`."""
    rows: list[dict[str, Any]] = []
    for sym, row in (current_positions or {}).items():
        su = str(sym).strip().upper()
        r = row or {}
        raw = r.get("notional")
        if raw is None or str(raw).strip() == "":
            raw = r.get("market_value", 0)
        try:
            mv = abs(float(raw or 0))
        except (TypeError, ValueError):
            mv = 0.0
        if mv <= 0:
            continue
        side = str(r.get("side", "long")).lower()
        row: dict[str, Any] = {"symbol": su, "market_value": mv, "side": side}
        for k in (
            "qty",
            "options_delta_notional",
            "options_delta_adjusted",
            "delta",
            "underlying_last",
            "underlying_price",
        ):
            v = r.get(k)
            if v is not None and str(v).strip() != "":
                row[k] = v
        rows.append(row)
    return rows


def _controlled_live_effective_max_total_exposure(
    config: dict[str, Any] | None,
    adaptive_cap_frac: float,
) -> float:
    """Apply the controlled-live portfolio cap to the adaptive gross-cap path."""

    try:
        eff = float(adaptive_cap_frac)
    except (TypeError, ValueError):
        eff = 0.0
    from .controlled_live_equity import controlled_live_equity_active, controlled_live_limits

    if not controlled_live_equity_active(config):
        return max(0.0, eff)
    limits = controlled_live_limits(config)
    cap_frac = float(limits.portfolio_exposure_cap_pct) / 100.0
    if cap_frac <= 0.0:
        return max(0.0, eff)
    return max(0.0, min(eff, cap_frac))


def score_conviction(
    entry_signal: Any,
    atr_pct: float | None,
    spread_pct: float | None,
    regime_label: str,
) -> str:
    """Heuristic conviction band from execution quality and regime (not entry strength)."""
    score = 0

    if getattr(entry_signal, "allowed", False):
        score += 40
    if atr_pct is not None and atr_pct < 2.5:
        score += 20
    if spread_pct is not None and spread_pct <= 0.3:
        score += 20
    _rl = str(regime_label or "").strip().lower()
    if _rl in {"bullish", "strong_risk_on"}:
        score += 20

    if score >= 80:
        return "strong"
    if score >= 60:
        return "medium"
    return "weak"


def _conviction_band_from_strength(strength: float | None) -> str | None:
    """Map entry strength (0–1 or 0–100) to labels used by :meth:`PositionSizer._apply_dynamic_multipliers`."""
    from src.options_config import conviction_band_from_entry_strength

    return conviction_band_from_entry_strength(strength)


@dataclass
class TradingEngineState:
    portfolio_risk: PortfolioRiskState = field(default_factory=PortfolioRiskState)
    execution: ExecutionState = field(default_factory=ExecutionState)
    pdt: PDTState = field(
        default_factory=lambda: PDTState(
            equity=0.0, day_trades_count_rolling=0, day_trade_dates=[], day_trade_log=[]
        )
    )
    breakout_trades_by_date: dict[str, int] = field(default_factory=dict)
    # After stop loss: block re-entry for cooldown; optional: require new breakout above stopped price
    last_stop_loss_at: dict[str, datetime] = field(default_factory=dict)
    last_stopped_ref_price: dict[str, float] = field(default_factory=dict)
    # After profit booking: cooldown and (unless strong_momentum_reentry_ok) require close > previous exit price
    last_profit_exit_at: dict[str, datetime] = field(default_factory=dict)
    last_profit_exit_price: dict[str, float] = field(default_factory=dict)
    # True when last profit-booking exit for this symbol was a partial (shorter re-entry cooldown).
    last_profit_exit_was_partial: dict[str, bool] = field(default_factory=dict)
    # After kill-switch exits: temporary re-entry hold, optionally bypassed by strong new signals.
    last_kill_switch_exit_at: dict[str, datetime] = field(default_factory=dict)


@dataclass
class TradeDecision:
    allowed: bool
    reason: str
    order_request: Any = None
    entry_signal: EntrySignal | None = None
    position_sizing: PositionSizingResult | None = None
    decision_report: str | None = None


def _entry_eval_route(entry_override: EntrySignal | None) -> str:
    """Short route label for ENTRY_EVAL (mirrors strategy-context ``source`` naming)."""
    if entry_override is None:
        return "trend"
    md0 = entry_override.metadata or {}
    if md0.get("source") == "news_sentiment":
        return "news_override"
    if md0.get("alternate_entry") or md0.get("source") in (
        "breakout",
        "mean_reversion",
        "volatility",
    ):
        return str(md0.get("source", "alternate"))
    return "entry_override"


def trade_decision_skips_entry(decision: TradeDecision | None) -> bool:
    """
    True when the live loop should treat the entry as **skip** (structured logs: ``decision: skip``).

    Callers must ``continue`` / return immediately and must not call the broker for this entry
    (no ``submit_order``, ``get_latest_quote`` for execution, option chain, etc.).
    """
    if decision is None:
        return True
    if not decision.allowed:
        return True
    if not decision.order_request:
        return True
    return False


class TradingEngine:
    """
    Single place to run all gates before sending a order.
    """

    def __init__(self, config: dict[str, Any] | None = None, config_path: str | Path | None = None):
        self.config = config or load_config(config_path)
        self.calendar = MarketCalendar(self.config)
        self.universe = UniverseFilter(self.config)
        self.market_quality = MarketQualityGate(self.config)
        self.strategy = TrendFollowingStrategy(self.config)
        self.sizer = PositionSizer(self.config)
        self.portfolio_risk = PortfolioRiskManager(self.config)
        self.execution = ExecutionManager(self.config)
        self.compliance = ComplianceManager(self.config)
        self.macro_blackout = MacroEventBlackout(self.config)
        self.earnings_blackout = EarningsBlackout(self.config)
        self.volatility_dnt = VolatilityDoNotTrade(self.config)
        self.state = TradingEngineState()

    def update_equity(self, equity: float, dt: datetime | None = None) -> None:
        dt = dt or datetime.utcnow()
        self.state.portfolio_risk.peak_equity = max(
            self.state.portfolio_risk.peak_equity,
            equity,
        )
        self.portfolio_risk.update_equity(self.state.portfolio_risk, dt, equity)
        self.compliance.update_equity(self.state.pdt, equity)

    def record_stop_loss(self, symbol: str, dt: datetime, entry_price: float | None = None) -> None:
        """Record a stop-loss exit so re-entry is subject to cooldown and optional new-breakout rule."""
        self.state.last_stop_loss_at[symbol] = dt
        if entry_price is not None:
            self.state.last_stopped_ref_price[symbol] = entry_price

    def record_profit_exit(
        self,
        symbol: str,
        dt: datetime,
        exit_price: float,
        *,
        after_partial: bool = False,
    ) -> None:
        """Record a profit-booking exit (take-profit, partial, trailing) for cooldown + optional re-entry price gate."""
        self.state.last_profit_exit_at[symbol] = dt
        self.state.last_profit_exit_price[symbol] = exit_price
        self.state.last_profit_exit_was_partial[symbol] = bool(after_partial)

    def record_kill_switch_exit(self, symbol: str, dt: datetime) -> None:
        """Record a kill-switch-driven exit so re-entry can pause briefly unless the next signal is strong."""
        self.state.last_kill_switch_exit_at[symbol] = dt

    def is_trading_allowed(self, dt: datetime) -> bool:
        return self.calendar.is_trading_allowed(dt)

    def run_entry_gates(
        self,
        symbol: str,
        dt: datetime,
        account_equity: float,
        current_positions: dict[str, Any],
        sector_exposure_pct: dict[str, float],
        # Market data for quality & strategy
        spread_pct: float,
        volume_atr_ratio: float | None = None,
        atr_pct: float | None = None,
        ohlcv_df: Any = None,
        symbol_sector: dict[str, str] | None = None,
        log_strategy_context: bool = False,
        regime_size_multiplier: float | None = None,
        entry_override: EntrySignal | None = None,
        regime_score: int | None = None,
        *,
        skip_spread_check: bool = False,
        regime_condition: str | None = None,
        gross_exposure_pct: float | None = None,
        net_exposure_pct: float | None = None,
        theme_exposure_pct: dict[str, float] | None = None,
        conviction: str | None = None,
        strategy_winrate: float | None = None,
        is_etf: bool | None = None,
        is_inverse_etf: bool | None = None,
        theme_key: str | None = None,
        regime_label: str | None = None,
        session_last_equity: float | None = None,
        cap_relax_factor: float = 1.0,
        entry_wave_strong_signal_count: int | None = None,
        dynamic_symbols: Iterable[str] | None = None,
        entry_route: str | None = None,
        dynamic_entry_momentum_score: float | None = None,
        dynamic_entry_breakout_continuation: bool = False,
    ) -> TradeDecision:
        """
        Run full gate sequence for an entry. Returns TradeDecision with allowed=False
        and reason if any gate fails.

        Optional ``gross_exposure_pct`` / ``net_exposure_pct`` / ``theme_exposure_pct`` override
        the internal :func:`~src.exposure.compute_exposures` snapshot when sizing (e.g. live loop
        book from the broker). ``theme_key`` selects the theme bucket in ``theme_exposure_pct``
        (defaults to ``THEME_MAP`` for the symbol). ``conviction`` / ``strategy_winrate`` /
        ``is_etf`` / ``is_inverse_etf`` override sizer inputs when provided.

        When ``conviction`` is omitted, conviction is :func:`score_conviction` using
        ``regime_label`` if set, else ``regime_condition``.

        Optional ``session_last_equity`` (broker prior close, e.g. Alpaca ``last_equity``) is
        forwarded to portfolio risk when ``live_daily_pnl_from_equity_snapshot`` is enabled.

        ``cap_relax_factor`` (≥1.0) widens market-quality and execution spread caps, gross
        exposure gate, and per-symbol/sector sizing from the live loop: cap-block streak relaxation
        (``adaptive.relax_caps_if_entry_blocked``) and/or strong-signal override
        (``adaptive.allow_cap_override_on_strong_signal``).

        Optional ``entry_wave_strong_signal_count`` (live entry scan) feeds
        ``adaptive.boost_exposure_if_many_signals`` when set in config.

        ``entry_route`` labels the live-loop path (e.g. ``momentum_breakout`` vs ``trend_long``).
        For ``momentum_breakout``, ``dynamic_momentum_entry.dynamic_atr_cap`` tightens volatility
        DNT and market-quality spike thresholds when set in config.
        """
        today = dt.date() if isinstance(dt, datetime) else date.today()

        if not self.calendar.is_trading_allowed(dt):
            return TradeDecision(allowed=False, reason="market closed or session not tradeable")

        macro = self.macro_blackout.check(dt)
        if not macro.allowed:
            return TradeDecision(allowed=False, reason=macro.reason)


        _sym_u = str(symbol).upper()

        _dynamic_symbols = {
            str(s).strip().upper()
            for s in (
                dynamic_symbols
                if dynamic_symbols is not None
                else getattr(self, "dynamic_symbols", set()) or set()
            )
            if s is not None and str(s).strip()
        }

        # Temporary safety fallback while dynamic universe wiring is being completed
        _dynamic_symbols.add("SMCI")

        if not self.universe.is_eligible(symbol) and _sym_u not in _dynamic_symbols:
            return TradeDecision(
                allowed=False,
                reason=f"symbol {symbol} not in universe or liquidity filter",
            )

        earnings = self.earnings_blackout.check(symbol, dt)
        if not earnings.allowed:
            return TradeDecision(allowed=False, reason=earnings.reason)

        _last_bar_vol = last_bar_volume_from_ohlcv(ohlcv_df)
        _ignore_spread_low_vol = self.market_quality.should_ignore_spread_for_low_volume(_last_bar_vol)
        _ignore_spread = bool(skip_spread_check or _ignore_spread_low_vol)
        _spread_for_gates = None if skip_spread_check else spread_pct

        try:
            _cap_rel = max(1.0, float(cap_relax_factor or 1.0))
        except (TypeError, ValueError):
            _cap_rel = 1.0
        _spike_atr_override: float | None = None
        _dme_atr = self.config.get("dynamic_momentum_entry") or {}
        if (
            entry_route == "momentum_breakout"
            and isinstance(_dme_atr, dict)
            and _dme_atr.get("dynamic_atr_cap") is not None
            and str(_dme_atr.get("dynamic_atr_cap")).strip() != ""
        ):
            try:
                _spike_atr_override = float(_dme_atr["dynamic_atr_cap"])
            except (TypeError, ValueError):
                _spike_atr_override = None
        mq = self.market_quality.check(
            symbol=symbol,
            spread_pct=_spread_for_gates,
            volume_atr_ratio=volume_atr_ratio,
            current_atr_pct=atr_pct,
            last_bar_volume=_last_bar_vol,
            cap_relax_factor=_cap_rel,
            is_dynamic_universe_symbol=_sym_u in _dynamic_symbols,
            volatility_spike_atr_cap_override=_spike_atr_override,
        )
        if not mq.ok:
            return TradeDecision(allowed=False, reason=f"market_quality: {mq.reason}")

        allowed, reason = self.execution.can_trade_spread(
            spread_pct,
            symbol,
            ignore_spread_gate=_ignore_spread,
            cap_relax_factor=_cap_rel,
        )
        if not allowed:
            return TradeDecision(allowed=False, reason=reason)

        vol_dnt = self.volatility_dnt.check(
            atr_pct=atr_pct,
            spread_pct=_spread_for_gates,
            symbol=symbol,
            regime_score=regime_score,
            last_bar_volume=_last_bar_vol,
            regime_condition=regime_condition,
            dynamic_symbols=_dynamic_symbols,
            entry_route=entry_route,
        )
        if not vol_dnt.allowed:
            return TradeDecision(allowed=False, reason=vol_dnt.reason)

        _tape = check_listed_defensive_tape_gates(
            self.config, symbol, ohlcv_df, atr_pct
        )
        if not _tape.allowed:
            return TradeDecision(allowed=False, reason=_tape.reason)

        if str(entry_route or "").strip().lower() == "news_trend_override":
            log.info(
                "NEWS_TREND_OVERRIDE_GATE symbol=%s skipped=trend_regime_filters route=%s",
                _sym_u,
                entry_route,
            )
        else:
            _trend = check_trend_regime_filters(
                self.config,
                symbol,
                ohlcv_df,
                momentum_score=dynamic_entry_momentum_score,
                breakout_continuation=bool(dynamic_entry_breakout_continuation),
            )
            if not _trend.allowed:
                return TradeDecision(allowed=False, reason=_trend.reason)

        if self.execution.should_block_strategy(self.state.execution):
            return TradeDecision(
                allowed=False,
                reason="strategy blocked: avg slippage exceeded threshold",
            )

        can_trade, reason = self.portfolio_risk.can_trade(
            self.state.portfolio_risk,
            account_equity,
            None,
            today,
            session_last_equity=session_last_equity,
        )
        if not can_trade:
            return TradeDecision(allowed=False, reason=reason)

        if gross_exposure_pct is not None and is_reduce_only_overexposed(
            float(gross_exposure_pct), self.config
        ):
            _thr_pct = float(resolve_reduce_only_gross_frac(self.config)) * 100.0
            return TradeDecision(
                allowed=False,
                reason=(
                    "over_exposed_mode: reduce_only (gross book %.1f%% of equity; threshold %.0f%%)"
                    % (float(gross_exposure_pct), _thr_pct)
                ),
            )

        ok_sym, sym_reason = self.portfolio_risk.can_enter_symbol_activity(
            self.state.portfolio_risk, symbol, today
        )
        if not ok_sym:
            return TradeDecision(allowed=False, reason=sym_reason)

        can_dt, reason = self.compliance.can_day_trade(self.state.pdt, today)
        if not can_dt:
            return TradeDecision(allowed=False, reason=reason)

        bypass_cooldown = self.strategy.strong_trend_reconfirm_ok(symbol, ohlcv_df, regime_score)

        # Cooldown after stop loss: avoid immediate re-entry
        if not bypass_cooldown and symbol in self.state.last_stop_loss_at:
            elapsed_min = (dt - self.state.last_stop_loss_at[symbol]).total_seconds() / 60.0
            if elapsed_min < self.strategy.cooldown_after_stop_minutes:
                return TradeDecision(
                    allowed=False,
                    reason=f"cooldown after stop loss ({elapsed_min:.0f} min < {self.strategy.cooldown_after_stop_minutes} min)",
                )

        # Optional: require new breakout above stopped price before re-entry
        if (
            self.strategy.require_new_breakout_after_stop
            and symbol in self.state.last_stopped_ref_price
            and ohlcv_df is not None
            and not ohlcv_df.empty
        ):
            ref_price = self.state.last_stopped_ref_price[symbol]
            current_close = float(ohlcv_df["close"].iloc[-1])
            if current_close <= ref_price:
                return TradeDecision(
                    allowed=False,
                    reason=f"re-entry requires new breakout above stopped price ({current_close:.2f} <= {ref_price:.2f})",
                )

        # Cooldown after profit booking (same symbol only; shorter after partial exits).
        if not bypass_cooldown and symbol in self.state.last_profit_exit_at:
            elapsed_min = (dt - self.state.last_profit_exit_at[symbol]).total_seconds() / 60.0
            _profit_cd = (
                float(self.strategy.cooldown_after_profit_partial_minutes)
                if self.state.last_profit_exit_was_partial.get(symbol)
                else float(self.strategy.cooldown_after_profit_minutes)
            )
            if elapsed_min < _profit_cd:
                return TradeDecision(
                    allowed=False,
                    reason=f"cooldown after profit booking ({elapsed_min:.0f} min < {_profit_cd:.0f} min)",
                )

        # Max(entries.per_symbol_sell_cooldown_min, execution post-exit) vs wall time since last stop/profit record
        su_up = str(symbol).strip().upper()
        ex_sym = float(symbol_post_exit_cooldown_minutes(su_up, self.config))
        ent_cfg = (self.config or {}).get("entries")
        _sell_entry_cd = float(
            parse_per_symbol_sell_cooldown_min(
                ent_cfg if isinstance(ent_cfg, dict) else None
            )
        )
        _req_post = max(_sell_entry_cd, ex_sym)
        if _req_post > 0 and not bypass_cooldown:
            _wall = minutes_since_last_broker_structured_exit(self.state, su_up, dt)
            if _wall is not None and _wall < _req_post:
                return TradeDecision(
                    allowed=False,
                    reason=(
                        "post-exit re-entry wait (%s min < %.0f min, entries+execution max)"
                        % (f"{_wall:.0f}", _req_post)
                    ),
                )

        # After profit: strict close > exit, or pullback buffer (entries.*), optional strong-structure bypass
        _momentum_reentry = self.strategy.strong_momentum_reentry_ok(symbol, ohlcv_df, regime_score)
        _allow_pb, _buf_pct = entries_reentry_pullback_cfg(self.config)
        if (
            not _momentum_reentry
            and symbol in self.state.last_profit_exit_price
            and ohlcv_df is not None
            and not ohlcv_df.empty
            and (
                self.strategy.require_price_above_exit_after_profit
                or _allow_pb
            )
        ):
            exit_price = self.state.last_profit_exit_price[symbol]
            current_close = float(ohlcv_df["close"].iloc[-1])
            _ok_px, _reason_px = profit_reentry_price_allowed(
                current_close,
                exit_price,
                require_price_above_exit_after_profit=self.strategy.require_price_above_exit_after_profit,
                allow_reentry_on_pullback=_allow_pb,
                reentry_price_buffer_pct=_buf_pct,
            )
            if not _ok_px:
                return TradeDecision(allowed=False, reason=_reason_px or "re-entry after profit blocked")

        _signal_breakdown: dict[str, object] = {}
        # Log strategy inputs so "no entry signal" is debuggable
        if log_strategy_context and ohlcv_df is not None and not ohlcv_df.empty:
            try:
                route = _entry_eval_route(entry_override)
                trend_b, _pullback_b, momentum_b, vol_b = self.strategy.entry_eval_components_for_log(
                    symbol,
                    ohlcv_df,
                    spread_pct,
                    atr_pct,
                    regime_score=regime_score,
                )
                _signal_breakdown = {
                    "symbol": str(symbol).strip().upper(),
                    "selected_route": "none",
                    "trend": bool(trend_b),
                    "pullback": bool(_pullback_b),
                    "momentum": bool(momentum_b),
                    "volume": bool(vol_b),
                    "spread": True,
                    "failed_hard_gates": [],
                    "noncritical_failures": [
                        name
                        for name, ok in {
                            "trend": trend_b,
                            "pullback": _pullback_b,
                            "momentum": momentum_b,
                            "volume": vol_b,
                        }.items()
                        if not bool(ok)
                    ],
                }
                # Spread / market-quality / execution spread gates already passed at this point.
                spread_ok = True
                log.info(
                    "%s ENTRY_EVAL route=%s trend=%s momentum=%s volatility=%s spread=%s",
                    str(symbol).strip().upper(),
                    route,
                    trend_b,
                    momentum_b,
                    vol_b,
                    spread_ok,
                )
                close = ohlcv_df["close"]
                n = len(close)
                last_close = float(close.iloc[-1]) if n else None
                ma_f = float(close.rolling(self.strategy.ma_fast).mean().iloc[-1]) if n >= self.strategy.ma_fast else None
                ma_s = float(close.rolling(self.strategy.ma_slow).mean().iloc[-1]) if n >= self.strategy.ma_slow else None
                log.info(
                    "strategy input %s: close=%.2f ma_fast(%d)=%s ma_slow(%d)=%s atr_pct=%s source=%s",
                    symbol,
                    last_close or 0.0,
                    self.strategy.ma_fast,
                    f"{ma_f:.2f}" if ma_f is not None else "n/a",
                    self.strategy.ma_slow,
                    f"{ma_s:.2f}" if ma_s is not None else "n/a",
                    f"{atr_pct:.2f}%" if atr_pct is not None else "n/a",
                    route,
                )
            except Exception as e:
                log.debug("strategy context log failed: %s", e)

        _spread_for_entry = None if skip_spread_check else spread_pct
        if entry_override is not None:
            entry = entry_override
        else:
            entry = self.strategy.generate_entry(
                symbol, ohlcv_df, _spread_for_entry, atr_pct, regime_score=regime_score
            )
            if entry is None:
                if _signal_breakdown:
                    final_reason = (
                        "entry_quality_score_below_threshold"
                        if _signal_breakdown.get("noncritical_failures")
                        else "strategy_route_not_applicable"
                    )
                    log.info(
                        "ENTRY_SIGNAL_BREAKDOWN symbol=%s selected_route=none trend_long_score=n/a "
                        "trend_long_threshold=n/a dynamic_momentum_score=n/a breakout_score=n/a "
                        "mean_reversion_score=n/a failed_hard_gates=%s noncritical_failures=%s final_reason=%s",
                        _signal_breakdown.get("symbol"),
                        ",".join(_signal_breakdown.get("failed_hard_gates") or []) or "none",
                        ",".join(_signal_breakdown.get("noncritical_failures") or []) or "none",
                        final_reason,
                    )
                    return TradeDecision(allowed=False, reason=final_reason)
                return TradeDecision(allowed=False, reason="strategy_route_not_applicable")

        # Long-only: skip short signals (bearish with no position = skip, not sell-short)
        if entry.side and entry.side.lower() not in ("long", "buy"):
            return TradeDecision(allowed=False, reason="long-only: skipping non-long signal")

        risk_cfg = (self.config or {}).get("risk")
        risk_cfg = risk_cfg if isinstance(risk_cfg, dict) else {}
        exec_cfg = (self.config or {}).get("execution")
        exec_cfg = exec_cfg if isinstance(exec_cfg, dict) else {}
        try:
            _ks_cd = float(risk_cfg.get("kill_switch_reentry_cooldown_minutes", 0) or 0.0)
        except (TypeError, ValueError):
            _ks_cd = 0.0
        try:
            _ks_strong_thr = float(
                exec_cfg.get(
                    "strong_reentry_threshold",
                    risk_cfg.get("strong_reentry_threshold", 0.65),
                )
                or 0.65
            )
        except (TypeError, ValueError):
            _ks_strong_thr = 0.65
        if _ks_cd > 0:
            _ks_last = self.state.last_kill_switch_exit_at.get(symbol)
            if _ks_last is not None:
                _elapsed_ks = (dt - _ks_last).total_seconds() / 60.0
                _strong_reentry = float(entry.strength) >= float(_ks_strong_thr)
                if _elapsed_ks < _ks_cd and not _strong_reentry:
                    return TradeDecision(
                        allowed=False,
                        reason=(
                            "kill-switch re-entry cooldown (%.0f min < %.0f min, strength %.2f < %.2f)"
                            % (_elapsed_ks, _ks_cd, float(entry.strength), float(_ks_strong_thr))
                        ),
                        entry_signal=entry,
                    )

        # Layer 1a — current gross long book vs max (before sizing / risk; fail fast on full book)
        sym_u = str(symbol).strip().upper()
        exposure_rows = _positions_rows_for_exposure(current_positions)
        _def_sec = parse_sector_config(self.config)["default_sector"]
        exp_snap = compute_exposures(
            account_equity,
            exposure_rows,
            symbol_sector if symbol_sector is not None else {},
            default_sector=_def_sec,
        )
        gross_pct = float(exp_snap.gross_pct) if gross_exposure_pct is None else float(gross_exposure_pct)
        net_pct = float(exp_snap.net_pct) if net_exposure_pct is None else float(net_exposure_pct)
        theme_dict = exp_snap.theme_pct if theme_exposure_pct is None else theme_exposure_pct
        _theme_key = theme_key if theme_key is not None else THEME_MAP.get(sym_u, sym_u)

        _eg_cfg = parse_portfolio_exposure_gates(self.config)
        _cap_relief = parse_strong_signal_cap_relief(self.config)
        _hard_block, _hard_reason = portfolio_hard_gross_blocks_new_entry(
            gross_pct, self.config
        )
        if _hard_block:
            return TradeDecision(
                allowed=False,
                reason=_hard_reason or "portfolio hard gross limit",
            )
        _eff_max_total = adaptive_effective_max_total_exposure(
            self.config,
            base_max_total_exposure_frac=float(_eg_cfg["max_total_exposure_frac"]),
            regime_score=regime_score,
            regime_condition=regime_condition,
            entry_wave_strong_signal_count=entry_wave_strong_signal_count,
        )
        _eff_max_total = _controlled_live_effective_max_total_exposure(
            self.config,
            _eff_max_total,
        )
        _block_total, _block_reason = block_new_entries_total_exposure(
            gross_pct,
            enabled=bool(_eg_cfg["enabled"]),
            max_total_exposure_frac=_eff_max_total,
            entry_strength=float(entry.strength),
            relief=_cap_relief,
            symbol_upper=sym_u,
            cap_relax_factor=_cap_rel,
        )
        if _block_total:
            return TradeDecision(allowed=False, reason=_block_reason or "exposure_gate: total book over cap")
        _tech_mult = entry_size_multiplier_tech_sector_over_cap(
            sym_u,
            exp_snap.sector_pct,
            symbol_sector if symbol_sector is not None else {},
            enabled=bool(_eg_cfg["enabled"]),
            max_tech_sector_exposure_frac=float(_eg_cfg["max_tech_sector_exposure_frac"]),
            tech_over_cap_size_multiplier=float(_eg_cfg["tech_over_cap_size_multiplier"]),
        )
        if _tech_mult != 1.0:
            _base_reg = regime_size_multiplier if regime_size_multiplier is not None else 1.0
            regime_size_multiplier = float(_base_reg) * float(_tech_mult)

        # Position sizing
        current_with_stops = [
            (p.get("notional", 0), p.get("stop_pct", 0))
            for p in current_positions.values()
        ]
        open_risk_pct = self.sizer.total_open_risk_pct(account_equity, current_with_stops)

        entry_price = (
            float(ohlcv_df["close"].iloc[-1])
            if ohlcv_df is not None and not getattr(ohlcv_df, "empty", True)
            else 0.0
        )
        stop_distance_pct = float(entry.stop_pct)

        if conviction is not None:
            conviction_for_sizer = conviction
        else:
            _reg_l = regime_label if regime_label is not None else regime_condition
            conviction_for_sizer = score_conviction(
                entry,
                atr_pct,
                spread_pct,
                str(_reg_l or ""),
            )
        is_etf_flag = sym_u in ETF_SYMBOLS if is_etf is None else bool(is_etf)
        is_inverse_etf_flag = sym_u in INVERSE_ETFS if is_inverse_etf is None else bool(is_inverse_etf)

        sizing = self.sizer.size_position(
            account_equity=account_equity,
            price=entry_price,
            stop_distance_pct=stop_distance_pct,
            symbol=symbol,
            current_positions=current_positions,
            sector_exposure_pct=sector_exposure_pct,
            symbol_sector=symbol_sector,
            atr_pct=atr_pct,
            regime_size_multiplier=regime_size_multiplier,
            regime_score=regime_score,
            conviction_score=entry.strength,
            current_drawdown_pct=self.portfolio_risk.current_drawdown_pct(
                self.state.portfolio_risk,
                account_equity,
            ),
            conviction=conviction_for_sizer,
            strategy_winrate=strategy_winrate,
            current_gross_exposure_pct=gross_pct,
            current_net_exposure_pct=net_pct,
            current_theme_exposure_pct=float(theme_dict.get(_theme_key, 0.0)),
            is_etf=is_etf_flag,
            is_inverse_etf=is_inverse_etf_flag,
            ohlcv_df=ohlcv_df,
            theme_key=_theme_key,
            cap_relax_factor=_cap_rel,
            regime_condition=regime_condition,
        )
        _soft_cfg = _eg_cfg.get("soft_cap")
        _soft_cfg = _soft_cfg if isinstance(_soft_cfg, dict) else {}
        _soft_cap_on = bool(_soft_cfg.get("enabled", False))
        if (
            _soft_cap_on
            and sizing.reject_reason == "portfolio caps leave no room"
            and entry_price > 0
            and account_equity > 0
        ):
            _ceil_pb = self.sizer.portfolio_budget_ceiling_additional_buy_usd(
                account_equity,
                gross_pct,
                net_pct,
                float(theme_dict.get(_theme_key, 0.0)),
                is_etf_flag,
                is_inverse_etf_flag,
                _theme_key,
                regime_condition=regime_condition,
                regime_score=regime_score,
            )
            _px_sc = float(entry_price)
            _sh_pb = int(_ceil_pb / _px_sc) if _px_sc > 0 else 0
            if _sh_pb < 1 and self.sizer._small_fill_one_share_ok(_ceil_pb, _px_sc):
                _sh_pb = 1
            if _sh_pb >= 1:
                _rps = _px_sc * (stop_distance_pct / 100.0)
                _not_pb = _sh_pb * _px_sc
                _ram = _sh_pb * _rps
                sizing = PositionSizingResult(
                    shares=_sh_pb,
                    notional=_not_pb,
                    risk_amount=_ram,
                    risk_pct=(float(_ram) / float(account_equity)) * 100.0,
                    exposure_pct=(float(_not_pb) / float(account_equity)) * 100.0,
                    trimmed=True,
                    trim_reason="portfolio_soft_cap_budget_headroom",
                    reject_reason=None,
                    confidence_score=None,
                )
        if _soft_cap_on and not sizing.reject_reason:
            _mx_sc = soft_cap_max_buy_usd(
                current_gross_pct=gross_pct,
                account_equity=float(account_equity),
                max_total_exposure_frac=float(_eff_max_total),
                exposure_gates_enabled=bool(_eg_cfg["enabled"]),
                relief=_cap_relief,
                symbol_upper=sym_u,
                entry_strength=float(entry.strength),
                cap_relax_factor=_cap_rel,
            )
            if float(sizing.notional) > float(_mx_sc) + 1e-6:
                sizing = clamp_position_sizing_to_max_buy_usd(
                    sizing,
                    max_buy_usd=float(_mx_sc),
                    price=float(entry_price),
                    account_equity=float(account_equity),
                )
        _mq_spread_scale = float(getattr(mq, "spread_position_scale", 1.0) or 1.0)
        if _mq_spread_scale < 1.0 - 1e-12 and not sizing.reject_reason:
            sizing = scale_position_sizing_by_multiplier(
                sizing,
                _mq_spread_scale,
                trim_reason="liquid_spread_relief",
                price=float(entry_price),
                account_equity=float(account_equity),
            )
        _entry_md = entry.metadata or {}
        _starter_notional_for_order: float | None = None
        if (
            isinstance(_entry_md, dict)
            and _entry_md.get("source") == "news_catalyst"
            and not sizing.reject_reason
        ):
            try:
                _starter_cap = float(
                    _entry_md.get("max_buy_notional_usd")
                    or _entry_md.get("starter_notional_usd")
                    or 750.0
                )
            except (TypeError, ValueError):
                _starter_cap = 750.0
            _starter_cap = max(500.0, min(750.0, _starter_cap))
            sizing = clamp_position_sizing_to_max_buy_usd(
                sizing,
                max_buy_usd=_starter_cap,
                price=float(entry_price),
                account_equity=float(account_equity),
            )
            if not sizing.reject_reason:
                _starter_notional_for_order = float(sizing.notional)
        if sizing.reject_reason:
            return TradeDecision(
                allowed=False,
                reason=sizing.reject_reason,
                entry_signal=entry,
                position_sizing=sizing,
            )
        # HARD: projected = current_gross + new_notional (fraction of equity) vs min(1.0, cap); no relief
        _hard_skip, _hard_r = hard_exposure_reject_if_projected_gross_exceeds_cap(
            current_gross_pct=gross_pct,
            buy_notional=float(sizing.notional),
            account_equity=float(account_equity),
            max_gross_cap_frac=_eff_max_total,
            exposure_gates_enabled=bool(_eg_cfg["enabled"]),
        )
        if _hard_skip:
            return TradeDecision(
                allowed=False,
                reason=_hard_r or "hard_exposure: projected gross over cap — skip buy",
                entry_signal=entry,
                position_sizing=sizing,
            )
        # Layer 1b — projected vs max with relief / relax (after HARD pass)
        _skip_proj, _skip_proj_r = layer1_reject_buy_if_projected_gross_exceeds_max(
            current_gross_pct=gross_pct,
            buy_notional=float(sizing.notional),
            account_equity=float(account_equity),
            enabled=bool(_eg_cfg["enabled"]),
            max_total_exposure_frac=_eff_max_total,
            entry_strength=float(entry.strength),
            relief=_cap_relief,
            symbol_upper=sym_u,
            cap_relax_factor=_cap_rel,
        )
        if _skip_proj:
            return TradeDecision(
                allowed=False,
                reason=_skip_proj_r or "exposure_gate: projected gross over max — skip buy",
                entry_signal=entry,
                position_sizing=sizing,
            )
        if self.sizer.would_exceed_max_open_risk(open_risk_pct, sizing.risk_pct):
            return TradeDecision(
                allowed=False,
                reason=f"would exceed max open risk ({self.sizer.max_open_risk_pct}%)",
                entry_signal=entry,
                position_sizing=sizing,
            )

        # Build order (limit preferred). Map long -> buy for execution/broker.
        mid = ohlcv_df["close"].iloc[-1] if ohlcv_df is not None and not ohlcv_df.empty else 0.0
        order_side = "buy" if (entry.side or "long").lower() in ("long", "buy") else "sell"
        if _starter_notional_for_order is not None and order_side == "buy":
            order = self.execution.build_order(
                symbol=symbol,
                side=order_side,
                quantity=0,
                mid_price=mid,
                spread_pct=spread_pct,
                ignore_spread_gate=_ignore_spread,
                cap_relax_factor=_cap_rel,
                notional=_starter_notional_for_order,
            )
        else:
            order = self.execution.build_order_for_entry(
                symbol=symbol,
                side=order_side,
                quantity=sizing.shares,
                mid_price=mid,
                spread_pct=spread_pct,
                ignore_spread_gate=_ignore_spread,
                cap_relax_factor=_cap_rel,
            )
        if order is None:
            reject_reason = getattr(
                self.execution,
                "last_order_build_reject_reason",
                None,
            )
            return TradeDecision(
                allowed=False,
                reason=(
                    f"execution: order build failed ({reject_reason})"
                    if reject_reason
                    else "execution: order build failed"
                ),
                entry_signal=entry,
                position_sizing=sizing,
            )

        # Clear stop-loss and profit-exit cooldown state for this symbol now that we allow entry
        self.state.last_stop_loss_at.pop(symbol, None)
        self.state.last_stopped_ref_price.pop(symbol, None)
        self.state.last_profit_exit_at.pop(symbol, None)
        self.state.last_profit_exit_price.pop(symbol, None)
        self.state.last_profit_exit_was_partial.pop(symbol, None)
        self.state.last_kill_switch_exit_at.pop(symbol, None)

        _trend_ok = _pullback_ok = _momentum_ok = _vol_ok = None
        if ohlcv_df is not None and not getattr(ohlcv_df, "empty", True):
            try:
                _trend_ok, _pullback_ok, _momentum_ok, _vol_ok = self.strategy.entry_eval_components_for_log(
                    symbol,
                    ohlcv_df,
                    spread_pct,
                    atr_pct,
                    regime_score=regime_score,
                )
            except Exception:
                pass
        _decision_report = build_buy_decision_report(
            symbol=symbol,
            route=_entry_eval_route(entry_override),
            trend_ok=_trend_ok,
            pullback_ok=_pullback_ok,
            momentum_ok=_momentum_ok,
            volatility_ok=_vol_ok,
            regime_score=regime_score,
            regime_condition=regime_condition,
            spread_pct=spread_pct,
            gross_exposure_pct=gross_pct,
            max_gross_exposure_frac=_eff_max_total,
            entry_signal=entry,
        )

        return TradeDecision(
            allowed=True,
            reason="ok",
            order_request=order,
            entry_signal=entry,
            position_sizing=sizing,
            decision_report=_decision_report,
        )

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
        log_exit_context: bool = False,
        smart_scale_out_index: int = 0,
        builtin_smart_trailing: bool = True,
        ohlcv_df: Any = None,
    ) -> ExitSignal | None:
        """atr_pct must be ATR% = (ATR/close)*100, not a ratio or multiple."""
        if log_exit_context:
            tp_hit, sl_hit, time_exit = self.strategy.exit_eval_flags_for_log(
                symbol,
                entry_price,
                current_price,
                bars_held,
                partial_taken=partial_taken,
                current_qty=current_qty,
                minutes_held=minutes_held,
                minutes_since_session_open_et=minutes_since_session_open_et,
            )
            log.info(
                "%s EXIT_EVAL tp_hit=%s sl_hit=%s time_exit=%s",
                str(symbol).strip().upper(),
                tp_hit,
                sl_hit,
                time_exit,
            )
        return self.strategy.check_exit(
            symbol,
            entry_price,
            current_price,
            bars_held,
            spread_pct,
            atr_pct,
            partial_taken=partial_taken,
            trail_high=trail_high,
            current_qty=current_qty,
            minutes_held=minutes_held,
            minutes_since_session_open_et=minutes_since_session_open_et,
            smart_scale_out_index=smart_scale_out_index,
            builtin_smart_trailing=builtin_smart_trailing,
            ohlcv_df=ohlcv_df,
        )
