"""
Trade filters: macro-event blackout, earnings blackout, volatility/spread do-not-trade.

- Macro blackout: no trading on configured dates or time windows (e.g. FOMC, CPI).
- Earnings blackout: no trading a symbol N days before/after its earnings date.
- Volatility/spread DNT: do not trade when ATR% or spread exceeds thresholds.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Iterable

from .universe import (
    liquid_spread_relief_parse,
    max_spread_pct_for_symbol_resolved,
    parse_symbol_max_spread_pct,
    spread_cap_tiers_from_mq,
    spread_volume_low_bypass,
    symbol_in_liquid_spread_relief_set,
    upper_symbol_frozenset,
)

logger = logging.getLogger(__name__)

try:
    import pytz
except ImportError:
    pytz = None


@dataclass
class FilterResult:
    allowed: bool
    reason: str


def _parse_time(s: str) -> time:
    parts = s.strip().split(":")
    return time(int(parts[0]), int(parts[1]) if len(parts) > 1 else 0)


def _parse_date(s: str) -> date:
    return date.fromisoformat(s.strip())


class MacroEventBlackout:
    """No trading during configured macro-event dates or time windows (ET)."""

    def __init__(self, config: dict[str, Any]):
        self.config = config
        tf = config.get("trade_filters", {})
        mb = tf.get("macro_blackout", {})
        self.enabled = bool(mb.get("enabled", True))
        self.blackout_dates: set[date] = set()
        for d in mb.get("blackout_dates", []):
            if isinstance(d, str):
                self.blackout_dates.add(_parse_date(d))
            elif hasattr(d, "year"):
                self.blackout_dates.add(d)
        self.blackout_windows: list[tuple[date, time, time]] = []
        for w in mb.get("blackout_windows", []):
            d = _parse_date(str(w.get("date", "")))
            start = _parse_time(str(w.get("start", "00:00")))
            end = _parse_time(str(w.get("end", "23:59")))
            self.blackout_windows.append((d, start, end))

    def check(self, dt: datetime) -> FilterResult:
        if not self.enabled:
            return FilterResult(allowed=True, reason="ok")
        d = dt.date() if hasattr(dt, "date") else dt
        t = dt.time() if hasattr(dt, "time") else time(0, 0)
        if d in self.blackout_dates:
            return FilterResult(allowed=False, reason=f"macro blackout date {d}")
        for win_date, start, end in self.blackout_windows:
            if d != win_date:
                continue
            if start <= end:
                if start <= t < end:
                    return FilterResult(allowed=False, reason=f"macro blackout window {d} {start}-{end}")
            else:
                if t >= start or t < end:
                    return FilterResult(allowed=False, reason=f"macro blackout window {d} {start}-{end}")
        return FilterResult(allowed=True, reason="ok")


class EarningsBlackout:
    """No trading a symbol N days before/after its earnings date."""

    def __init__(self, config: dict[str, Any]):
        tf = config.get("trade_filters", {})
        eb = tf.get("earnings_blackout", {})
        self.enabled = bool(eb.get("enabled", True))
        self.days_before = int(eb.get("days_before", 1))
        self.days_after = int(eb.get("days_after", 1))
        self.earnings_dates: dict[str, list[date]] = {}
        for sym, dates in eb.get("earnings_dates", {}).items():
            self.earnings_dates[sym.upper()] = [_parse_date(str(x)) for x in dates] if dates else []

    def check(self, symbol: str, dt: datetime) -> FilterResult:
        if not self.enabled:
            return FilterResult(allowed=True, reason="ok")
        d = dt.date() if hasattr(dt, "date") else dt
        sym = symbol.upper()
        for ed in self.earnings_dates.get(sym, []):
            start = ed - timedelta(days=self.days_before)
            end = ed + timedelta(days=self.days_after)
            if start <= d <= end:
                return FilterResult(allowed=False, reason=f"earnings blackout {symbol} around {ed}")
        return FilterResult(allowed=True, reason="ok")


class VolatilityDoNotTrade:
    """Do not trade when volatility (ATR%) or spread exceeds thresholds.
    Uses real ATR% (not ATR multiple); default cap when YAML omits ``max_atr_pct`` is **6.0**.
    Spread threshold is tiered: core vs high_vol."""

    def __init__(self, config: dict[str, Any]):
        self.config = config
        tf = config.get("trade_filters", {})
        vd = tf.get("volatility_do_not_trade", {})
        self.enabled = bool(vd.get("enabled", True))
        self.max_atr_pct = float(vd.get("max_atr_pct", 6.0))
        _mab = vd.get("max_atr_pct_bullish_regime")
        if _mab is not None and str(_mab).strip() != "":
            self.max_atr_pct_bullish_regime = float(_mab)
        else:
            self.max_atr_pct_bullish_regime = self.max_atr_pct
        self.bullish_regime_min_score = int(vd.get("bullish_regime_min_score", 4))
        # Default 0.5% for core; 0.10/0.15% is too strict for IEX
        self.max_spread_pct = float(vd.get("max_spread_pct", 0.5))
        high_vol = vd.get("high_vol_symbols") or []
        self.high_vol_symbols = {s.upper().strip() for s in high_vol if s}
        self.high_vol_max_spread_pct = float(vd.get("high_vol_max_spread_pct", 1.0))
        _vbt = vd.get("ignore_spread_when_last_bar_volume_below")
        if _vbt is None or str(_vbt).strip() == "":
            _vbt = (config.get("market_quality") or {}).get("ignore_spread_when_last_bar_volume_below")
        if _vbt is not None and str(_vbt).strip() != "":
            self.ignore_spread_when_last_bar_volume_below = float(_vbt)
        else:
            self.ignore_spread_when_last_bar_volume_below = None

        mr = config.get("market_regime") or {}
        _ahv = mr.get("allow_high_volatility_min_regime_score")
        if _ahv is not None and str(_ahv).strip() != "":
            self.allow_high_volatility_min_regime_score = int(_ahv)
        else:
            self.allow_high_volatility_min_regime_score = None

        _atr_reg = vd.get("max_atr_pct_regime") or {}
        self.max_atr_pct_regime: dict[str, float] | None = None
        if isinstance(_atr_reg, dict) and _atr_reg:
            self.max_atr_pct_regime = {
                str(k).strip().lower(): float(v)
                for k, v in _atr_reg.items()
                if k is not None and str(k).strip() != "" and v is not None
            }

        mq = config.get("market_quality") or {}
        self.symbol_max_spread_pct = parse_symbol_max_spread_pct(mq)
        self._spread_cap_tiers = spread_cap_tiers_from_mq(mq)
        self._high_vol_union = frozenset(self.high_vol_symbols) | upper_symbol_frozenset(
            mq.get("high_vol_symbols")
        )
        (
            self._lq_relief_enabled,
            self._lq_relief_symbols,
            self._lq_relief_hard_max,
            _,
        ) = liquid_spread_relief_parse(config)

    def _max_spread_for_symbol(self, symbol: str | None) -> float:
        return max_spread_pct_for_symbol_resolved(
            symbol,
            symbol_max_spread_pct=self.symbol_max_spread_pct,
            spread_tiers=self._spread_cap_tiers,
            high_vol_union=self._high_vol_union,
            legacy_max_spread_pct=self.max_spread_pct,
            legacy_high_vol_max_spread_pct=self.high_vol_max_spread_pct,
        )

    def check(
        self,
        atr_pct: float | None = None,
        spread_pct: float | None = None,
        symbol: str | None = None,
        regime_score: int | None = None,
        last_bar_volume: float | None = None,
        regime_condition: str | None = None,
        dynamic_symbols: Iterable[str] | None = None,
        *,
        entry_route: str | None = None,
    ) -> FilterResult:
        if not self.enabled:
            return FilterResult(allowed=True, reason="ok")
        cap_atr = self.max_atr_pct
        ckey = (regime_condition or "").strip().lower()
        if self.max_atr_pct_regime and ckey in self.max_atr_pct_regime:
            cap_atr = float(self.max_atr_pct_regime[ckey])
        else:
            thr = (
                self.allow_high_volatility_min_regime_score
                if self.allow_high_volatility_min_regime_score is not None
                else self.bullish_regime_min_score
            )
            if regime_score is not None and int(regime_score) >= thr:
                cap_atr = self.max_atr_pct_bullish_regime
        threshold = cap_atr
        vol_flag = bool(atr_pct is not None and float(atr_pct) > threshold)
        if atr_pct is not None:
            logger.debug(
                "ATR=%s, threshold=%s, vol_flag=%s",
                atr_pct,
                threshold,
                vol_flag,
            )
		

        # Dynamic momentum names naturally carry higher ATR%.
        # Prefer runtime scanner picks over broad config buckets, so core/leader names do not
        # accidentally inherit dynamic-only ATR caps.
        _du_cfg = self.config.get("dynamic_universe") or {}
        if dynamic_symbols is not None:
            _dynamic_symbols = {
                str(s).strip().upper()
                for s in dynamic_symbols
                if s is not None and str(s).strip()
            }
        else:
            _dynamic_symbols = {
                str(s).strip().upper()
                for s in (_du_cfg.get("active_runtime_symbols") or [])
                if s is not None and str(s).strip()
            }

        _is_dynamic_symbol = bool(
            symbol and symbol.upper() in _dynamic_symbols
        )

        _dme_cfg = self.config.get("dynamic_momentum_entry") or {}
        _dynamic_max_atr_pct = float(_du_cfg.get("max_atr_pct", 12.0))
        if (
            entry_route == "momentum_breakout"
            and isinstance(_dme_cfg, dict)
            and _dme_cfg.get("dynamic_atr_cap") is not None
            and str(_dme_cfg.get("dynamic_atr_cap")).strip() != ""
        ):
            try:
                _dynamic_max_atr_pct = float(_dme_cfg["dynamic_atr_cap"])
            except (TypeError, ValueError):
                pass

        _effective_cap_atr = (
            _dynamic_max_atr_pct
            if _is_dynamic_symbol
            else cap_atr
        )

        print(
            f"DYNAMIC_ATR_DEBUG "
            f"symbol={symbol} "
            f"is_dynamic={_is_dynamic_symbol} "
            f"atr={atr_pct} "
            f"cap={_effective_cap_atr} "
            f"runtime={_du_cfg.get('active_runtime_symbols')}",
            flush=True,
        )

        if atr_pct is not None and atr_pct > _effective_cap_atr:
            return FilterResult(
                allowed=False,
                reason=(
                    f"volatility DNT: ATR% {atr_pct:.2f} > "
                    f"{_effective_cap_atr}"
                ),
            )

        max_spread = self._max_spread_for_symbol(symbol)

        if spread_pct is not None and spread_pct > max_spread:
            if spread_volume_low_bypass(self.ignore_spread_when_last_bar_volume_below, last_bar_volume):
                pass
            elif (
                self._lq_relief_enabled
                and symbol_in_liquid_spread_relief_set(symbol, self._lq_relief_symbols)
                and (
                    self._lq_relief_hard_max is None
                    or float(spread_pct) <= float(self._lq_relief_hard_max) + 1e-9
                )
            ):
                pass
            else:
                return FilterResult(
                    allowed=False,
                    reason=f"volatility DNT: spread {spread_pct:.2f}% > {max_spread}",
                )
        return FilterResult(allowed=True, reason="ok")
