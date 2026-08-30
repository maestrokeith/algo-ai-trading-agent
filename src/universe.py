"""
Universe & data rules: high-liquidity symbols, market sessions, market quality gates.

- Trade only high-liquidity symbols (e.g. S&P 500 / top-volume ETFs).
- Use official market sessions: pre-market, regular, after-hours; handle holidays/half-days.
- Gate every trade on market quality: spread %, min volume/ATR, optional news spike block.
"""
import math
from dataclasses import dataclass
from datetime import date, time, datetime
from enum import Enum
from typing import Any

import pandas as pd


def last_bar_volume_from_ohlcv(ohlcv_df: Any) -> float | None:
    """Latest bar volume from a daily (or other) OHLCV frame, if present."""
    if ohlcv_df is None or getattr(ohlcv_df, "empty", True):
        return None
    if "volume" not in ohlcv_df.columns:
        return None
    try:
        v = ohlcv_df["volume"].iloc[-1]
        return float(v) if v is not None and str(v).strip() != "" else None
    except (TypeError, ValueError, IndexError):
        return None


def spread_volume_low_bypass(threshold: float | None, last_bar_volume: float | None) -> bool:
    """If last-bar share volume is strictly below *threshold*, skip spread max checks (optional)."""
    if threshold is None or last_bar_volume is None:
        return False
    return float(last_bar_volume) < float(threshold)


@dataclass(frozen=True)
class SpreadCapTiers:
    """Three-tier spread caps from ``market_quality.spread_caps`` (values are percent, e.g. 1.5 = 1.5%%).

    YAML may use ``mega_caps`` / ``large_caps``, ``high_vol`` / ``volatile`` for the outer tiers.
    """

    mega_caps: float
    normal: float
    high_vol: float
    mega_cap_symbols: frozenset[str]


_DEFAULT_MEGA_CAP_SYMBOLS = frozenset(
    {
        "SPY",
        "QQQ",
        "IWM",
        "DIA",
        "AAPL",
        "MSFT",
        "GOOGL",
        "GOOG",
        "AMZN",
        "NVDA",
        "META",
        "TSLA",
        "AVGO",
        "BRK.B",
        "BRKB",
        "LLY",
        "JPM",
        "V",
        "UNH",
        "WMT",
        "XOM",
        "MA",
        "PG",
        "JNJ",
        "COST",
        "HD",
    }
)


def upper_symbol_frozenset(raw: Any) -> frozenset[str]:
    """Build an uppercased frozenset from a YAML list/tuple/set of tickers."""
    if not raw:
        return frozenset()
    if not isinstance(raw, (list, tuple, set)):
        return frozenset()
    return frozenset(str(s).strip().upper() for s in raw if s is not None and str(s).strip())


def parse_symbol_max_spread_pct(mq: dict[str, Any]) -> dict[str, float]:
    out: dict[str, float] = {}
    _sms = mq.get("symbol_max_spread_pct") or {}
    if isinstance(_sms, dict):
        for k, v in _sms.items():
            if k is None or str(k).strip() == "" or v is None:
                continue
            out[str(k).strip().upper()] = float(v)
    return out


def spread_cap_tiers_from_mq(mq: dict[str, Any]) -> SpreadCapTiers | None:
    """
    If ``market_quality.spread_caps`` defines tier keys, return tiers; else ``None`` (legacy caps only).

    YAML keys: ``mega_caps`` (alias ``large_caps``), ``normal``, ``high_vol`` (alias ``volatile``);
    optional ``mega_cap_symbols`` list (defaults to a broad mega-cap / major ETF set).
    """
    raw = mq.get("spread_caps")
    if not isinstance(raw, dict):
        return None
    tier_keys = {
        "mega_caps",
        "mega_cap",
        "large_caps",
        "large_cap",
        "normal",
        "high_vol",
        "high_vol_cap",
        "volatile",
    }
    if not tier_keys.intersection(raw.keys()):
        return None
    mega = float(
        raw.get("mega_caps", raw.get("mega_cap", raw.get("large_caps", raw.get("large_cap", 1.5))))
    )
    normal = float(raw.get("normal", 2.5))
    high = float(raw.get("high_vol", raw.get("high_vol_cap", raw.get("volatile", 4.0))))
    mega_syms = upper_symbol_frozenset(raw.get("mega_cap_symbols"))
    if not mega_syms:
        mega_syms = _DEFAULT_MEGA_CAP_SYMBOLS
    return SpreadCapTiers(
        mega_caps=mega,
        normal=normal,
        high_vol=high,
        mega_cap_symbols=mega_syms,
    )


def spread_relief_scale_factor(
    spread_pct: float,
    cap: float,
    *,
    min_scale: float = 0.15,
) -> float:
    """
    Scale factor when bid/ask spread exceeds *cap* but trade is still allowed (liquid-symbol relief).

    Uses ``cap / spread_pct``, floored at *min_scale* and capped at 1.0.
    """
    try:
        sp = float(spread_pct)
        cp = float(cap)
        mn = float(min_scale)
    except (TypeError, ValueError):
        return 1.0
    if not (math.isfinite(sp) and math.isfinite(cp)) or cp <= 0 or sp <= 0:
        return 1.0
    if sp <= cp:
        return 1.0
    mn = max(0.01, min(1.0, mn))
    raw = cp / sp
    return max(mn, min(1.0, raw))


def liquid_spread_relief_parse(
    config: dict[str, Any] | None,
) -> tuple[bool, frozenset[str], float | None, float]:
    """
    Returns ``(enabled, symbol_set, hard_max_spread_pct_or_None, min_position_scale)``.

    When *enabled* is False, *symbol_set* is empty. *hard_max_spread_pct* rejects above this even with relief.
    """
    mq = (config or {}).get("market_quality") or {}
    lr = mq.get("liquid_spread_relief")
    if not isinstance(lr, dict) or not bool(lr.get("enabled", False)):
        return (False, frozenset(), None, 0.15)
    out: set[str] = set()
    if bool(lr.get("include_mega_cap_tiers", True)):
        tiers = spread_cap_tiers_from_mq(mq)
        if tiers is not None:
            out |= set(tiers.mega_cap_symbols)
    out |= set(upper_symbol_frozenset(lr.get("extra_symbols")))
    hard_raw = lr.get("hard_max_spread_pct")
    hard_max: float | None = None
    if hard_raw is not None and str(hard_raw).strip() != "":
        try:
            hard_max = float(hard_raw)
        except (TypeError, ValueError):
            hard_max = None
    try:
        min_scale = float(lr.get("min_position_scale", 0.15))
    except (TypeError, ValueError):
        min_scale = 0.15
    min_scale = max(0.01, min(1.0, min_scale))
    return (True, frozenset(out), hard_max, min_scale)


def symbol_in_liquid_spread_relief_set(symbol: str | None, symbol_set: frozenset[str]) -> bool:
    u = str(symbol or "").strip().upper()
    return bool(u and u in symbol_set)


def max_spread_pct_for_symbol_resolved(
    symbol: str | None,
    *,
    symbol_max_spread_pct: dict[str, float],
    spread_tiers: SpreadCapTiers | None,
    high_vol_union: frozenset[str],
    legacy_max_spread_pct: float,
    legacy_high_vol_max_spread_pct: float,
) -> float:
    """
    Resolve max bid/ask spread %% for *symbol*.

    Precedence: per-symbol override → (if tiers) high-vol / mega / normal → legacy core vs high-vol.
    """
    u = str(symbol or "").strip().upper()
    if u and u in symbol_max_spread_pct:
        return float(symbol_max_spread_pct[u])
    if spread_tiers is not None:
        if u and u in high_vol_union:
            return float(spread_tiers.high_vol)
        if u and u in spread_tiers.mega_cap_symbols:
            return float(spread_tiers.mega_caps)
        return float(spread_tiers.normal)
    if u and u in high_vol_union:
        return float(legacy_high_vol_max_spread_pct)
    return float(legacy_max_spread_pct)


class SessionType(Enum):
    PRE_MARKET = "pre_market"
    REGULAR = "regular"
    AFTER_HOURS = "after_hours"
    CLOSED = "closed"


@dataclass
class SessionWindow:
    start: time
    end: time
    trade_allowed: bool


@dataclass
class MarketQualityResult:
    ok: bool
    reason: str
    spread_pct: float | None = None
    volume_atr_ratio: float | None = None
    volatility_spike: bool = False
    #: When spread exceeds tier cap on a liquid-relief symbol, scale shares by this (default 1.0).
    spread_position_scale: float = 1.0


class MarketCalendar:
    """Market sessions and holiday handling (ET assumed for US equity)."""

    def __init__(self, config: dict[str, Any]):
        sessions = config.get("market_sessions", {})
        self.sessions = {
            SessionType.PRE_MARKET: self._parse_session(sessions.get("pre_market", {})),
            SessionType.REGULAR: self._parse_session(sessions.get("regular", {})),
            SessionType.AFTER_HOURS: self._parse_session(sessions.get("after_hours", {})),
        }
        self.holidays: set[date] = set()
        for d in config.get("holidays", []):
            if isinstance(d, str):
                self.holidays.add(date.fromisoformat(d))
            elif hasattr(d, "date"):
                self.holidays.add(d)

    @staticmethod
    def _parse_session(raw: dict) -> SessionWindow:
        def parse_time(s: str) -> time:
            parts = s.split(":")
            return time(int(parts[0]), int(parts[1]) if len(parts) > 1 else 0)

        return SessionWindow(
            start=parse_time(raw.get("start", "09:30")),
            end=parse_time(raw.get("end", "16:00")),
            trade_allowed=raw.get("trade_allowed", True),
        )

    def get_session_at(self, dt: datetime) -> SessionType:
        t = dt.time() if hasattr(dt, "time") else dt
        d = dt.date() if hasattr(dt, "date") else date.today()
        if d in self.holidays:
            return SessionType.CLOSED
        if self._in_window(t, self.sessions[SessionType.PRE_MARKET]):
            return SessionType.PRE_MARKET
        if self._in_window(t, self.sessions[SessionType.REGULAR]):
            return SessionType.REGULAR
        if self._in_window(t, self.sessions[SessionType.AFTER_HOURS]):
            return SessionType.AFTER_HOURS
        return SessionType.CLOSED

    @staticmethod
    def _in_window(t: time, w: SessionWindow) -> bool:
        if w.start <= w.end:
            return w.start <= t < w.end
        return t >= w.start or t < w.end

    def is_trading_allowed(self, dt: datetime) -> bool:
        session = self.get_session_at(dt)
        if session == SessionType.CLOSED:
            return False
        return self.sessions[session].trade_allowed

    def add_holiday(self, d: date) -> None:
        self.holidays.add(d)


class UniverseFilter:
    """Filter symbols by liquidity (min dollar volume, etc.)."""

    def __init__(self, config: dict[str, Any]):
        u = config.get("universe", {})
        self.symbols = list(u.get("symbols", []))
        self.min_avg_dollar_volume_30d = float(u.get("min_avg_dollar_volume_30d", 50_000_000))
        self.min_atr_multiple_for_volume = float(u.get("min_atr_multiple_for_volume", 0.5))

    def is_eligible(
        self,
        symbol: str,
        avg_dollar_volume_30d: float | None = None,
        volume_vs_atr: float | None = None,
    ) -> bool:
        if symbol not in self.symbols:
            return False
        if avg_dollar_volume_30d is not None and avg_dollar_volume_30d < self.min_avg_dollar_volume_30d:
            return False
        if volume_vs_atr is not None and volume_vs_atr < self.min_atr_multiple_for_volume:
            return False
        return True


class MarketQualityGate:
    """Gate every trade on spread %, min volume/ATR, optional news/volatility spike.
    Spread threshold: optional ``spread_caps`` (mega / normal / high_vol), else legacy core vs high_vol."""
    def __init__(self, config: dict[str, Any]):
        mq = config.get("market_quality", {})
        # Default 0.5% — 0.10% is too strict for IEX / intraday
        self.max_spread_pct = float(mq.get("max_spread_pct", 0.5))
        self.min_volume_atr_ratio = float(mq.get("min_volume_atr_ratio", 1.0))
        self.block_on_news_spike = bool(mq.get("block_on_news_spike", True))
        # Threshold is ATR% (e.g. 8.0 = block when daily ATR% >= 8%). Dynamic scanner names use the higher cap.
        self.news_volatility_spike_atr_pct = float(
            mq.get("news_volatility_spike_atr_pct") or mq.get("news_volatility_spike_atr_multiple", 2.0)
        )
        _raw_spike_dyn = mq.get("news_volatility_spike_atr_pct_dynamic")
        if _raw_spike_dyn is not None and str(_raw_spike_dyn).strip() != "":
            try:
                self.news_volatility_spike_atr_pct_dynamic = float(_raw_spike_dyn)
            except (TypeError, ValueError):
                self.news_volatility_spike_atr_pct_dynamic = 14.0
        else:
            self.news_volatility_spike_atr_pct_dynamic = 14.0
        high_vol = mq.get("high_vol_symbols") or []
        self.high_vol_symbols = {s.upper().strip() for s in high_vol if s}
        self.high_vol_max_spread_pct = float(mq.get("high_vol_max_spread_pct", 1.0))
        self.symbol_max_spread_pct = parse_symbol_max_spread_pct(mq)
        self._spread_cap_tiers = spread_cap_tiers_from_mq(mq)
        self._mq_high_vol_frozen = frozenset(self.high_vol_symbols)
        _vbt = mq.get("ignore_spread_when_last_bar_volume_below")
        if _vbt is not None and str(_vbt).strip() != "":
            self.ignore_spread_when_last_bar_volume_below = float(_vbt)
        else:
            self.ignore_spread_when_last_bar_volume_below = None
        (
            self._liquid_relief_enabled,
            self._liquid_relief_symbols,
            self._liquid_relief_hard_max_spread,
            self._liquid_relief_min_scale,
        ) = liquid_spread_relief_parse(config)

    def should_ignore_spread_for_low_volume(self, last_bar_volume: float | None) -> bool:
        return spread_volume_low_bypass(self.ignore_spread_when_last_bar_volume_below, last_bar_volume)

    def _max_spread_for_symbol(self, symbol: str | None) -> float:
        return max_spread_pct_for_symbol_resolved(
            symbol,
            symbol_max_spread_pct=self.symbol_max_spread_pct,
            spread_tiers=self._spread_cap_tiers,
            high_vol_union=self._mq_high_vol_frozen,
            legacy_max_spread_pct=self.max_spread_pct,
            legacy_high_vol_max_spread_pct=self.high_vol_max_spread_pct,
        )

    def check(
        self,
        symbol: str | None = None,
        spread_pct: float | None = None,
        volume_atr_ratio: float | None = None,
        current_atr_pct: float | None = None,
        last_bar_volume: float | None = None,
        cap_relax_factor: float = 1.0,
        *,
        is_dynamic_universe_symbol: bool = False,
        volatility_spike_atr_cap_override: float | None = None,
    ) -> MarketQualityResult:
        try:
            _f = max(1.0, float(cap_relax_factor or 1.0))
        except (TypeError, ValueError):
            _f = 1.0
        max_spread = min(100.0, self._max_spread_for_symbol(symbol) * _f)
        spread_scale = 1.0
        if spread_pct is not None and spread_pct > max_spread:
            if self.should_ignore_spread_for_low_volume(last_bar_volume):
                pass
            elif (
                self._liquid_relief_enabled
                and symbol_in_liquid_spread_relief_set(symbol, self._liquid_relief_symbols)
            ):
                if (
                    self._liquid_relief_hard_max_spread is not None
                    and float(spread_pct) > float(self._liquid_relief_hard_max_spread)
                ):
                    return MarketQualityResult(
                        ok=False,
                        reason=(
                            f"spread {float(spread_pct):.4f}% > hard max "
                            f"{float(self._liquid_relief_hard_max_spread):.4f}%"
                        ),
                        spread_pct=float(spread_pct),
                    )
                spread_scale = spread_relief_scale_factor(
                    float(spread_pct),
                    float(max_spread),
                    min_scale=self._liquid_relief_min_scale,
                )
            else:
                return MarketQualityResult(
                    ok=False,
                    reason=f"spread {spread_pct:.4f}% > max {max_spread}%",
                    spread_pct=spread_pct,
                )
        if volume_atr_ratio is not None and volume_atr_ratio < self.min_volume_atr_ratio:
            return MarketQualityResult(
                ok=False,
                reason=f"volume/ATR {volume_atr_ratio:.4f} < min {self.min_volume_atr_ratio}",
                volume_atr_ratio=volume_atr_ratio,
            )
        if volatility_spike_atr_cap_override is not None and str(
            volatility_spike_atr_cap_override
        ).strip() != "":
            try:
                spike_cap = float(volatility_spike_atr_cap_override)
            except (TypeError, ValueError):
                spike_cap = (
                    float(self.news_volatility_spike_atr_pct_dynamic)
                    if is_dynamic_universe_symbol
                    else float(self.news_volatility_spike_atr_pct)
                )
        else:
            spike_cap = (
                float(self.news_volatility_spike_atr_pct_dynamic)
                if is_dynamic_universe_symbol
                else float(self.news_volatility_spike_atr_pct)
            )
        if (
            self.block_on_news_spike
            and current_atr_pct is not None
            and current_atr_pct >= spike_cap
        ):
            return MarketQualityResult(
                ok=False,
                reason=f"volatility spike: ATR% {current_atr_pct:.2f} >= {spike_cap}%",
                volatility_spike=True,
            )
        return MarketQualityResult(
            ok=True,
            reason="ok",
            spread_position_scale=spread_scale,
        )
