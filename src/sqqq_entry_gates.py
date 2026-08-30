"""
SQQQ entry refinements for low regime scores (e.g. score 2): optional intraday/daily
filters without requiring a fresh MA cross.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)


def market_regime_entry_policy_cfg(config: dict) -> dict[str, Any]:
    return (config.get("market_regime") or {}).get("entry_policy") or {}


def score_2_skip_fresh_cross_below_ma(config: dict, regime_score: int | None) -> bool:
    """True when score is 2 and YAML opts out of the global fresh-cross rule for SQQQ."""
    if regime_score != 2:
        return False
    ep = market_regime_entry_policy_cfg(config)
    return bool(ep.get("score_2_sqqq_skip_fresh_cross", True))


def _qqq_ma20_below_ma50(broker: Any, symbol: str = "QQQ") -> bool | None:
    df = broker.get_bars(symbol, timeframe="1Day", limit=60)
    if df is None or getattr(df, "empty", True) or len(df) < 50:
        return None
    close = df["close"]
    ma20 = float(close.rolling(20).mean().iloc[-1])
    ma50 = float(close.rolling(50).mean().iloc[-1])
    return ma20 < ma50


def _qqq_red_day(
    broker: Any,
    *,
    symbol: str = "QQQ",
    last_price: float | None,
) -> bool | None:
    """Today's regular-session proxy: last price below today's daily open (partial bar)."""
    if last_price is None:
        return None
    df = broker.get_bars(symbol, timeframe="1Day", limit=2)
    if df is None or getattr(df, "empty", True):
        return None
    try:
        open_today = float(df["open"].iloc[-1])
    except (TypeError, ValueError, KeyError):
        return None
    return float(last_price) < open_today


def _qqq_below_session_vwap(broker: Any, *, symbol: str = "QQQ", now_et: datetime) -> bool | None:
    """Intraday VWAP from 1m bars since today's 9:30 ET vs last bar close."""
    try:
        import pytz
    except ImportError:
        return None
    et = pytz.timezone("America/New_York")
    if now_et.tzinfo is None:
        now_et = et.localize(now_et)
    else:
        now_et = now_et.astimezone(et)
    # Regular session start
    sod = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    if now_et < sod:
        return None
    start_utc = sod.astimezone(timezone.utc)
    end_utc = datetime.now(timezone.utc)
    df = broker.get_bars(symbol, timeframe="1Min", start=start_utc, end=end_utc, limit=500)
    if df is None or getattr(df, "empty", True) or len(df) < 3:
        return None
    vol = df["volume"].astype(float)
    if vol.sum() <= 0:
        return None
    tp = (df["high"].astype(float) + df["low"].astype(float) + df["close"].astype(float)) / 3.0
    vwap = float((tp * vol).sum() / vol.sum())
    last = float(df["close"].iloc[-1])
    return last < vwap


def score_2_sqqq_optional_filters_pass(
    config: dict,
    broker: Any,
    *,
    now_et: datetime,
    qqq_last: float | None,
) -> tuple[bool, str]:
    """
    If no optional filters enabled in YAML → (True, "").

    If any enabled → **any** may satisfy (OR). Each enabled check that returns
    None (missing data) does not count as pass (nor as fail for the whole OR —
    we need at least one True among enabled checks with definitive results).

    If all enabled checks are None → block with reason.
    """
    ep = market_regime_entry_policy_cfg(config)
    use_v = bool(ep.get("score_2_sqqq_filter_below_vwap", False))
    use_red = bool(ep.get("score_2_sqqq_filter_red_day", False))
    use_ma = bool(ep.get("score_2_sqqq_filter_ma20_below_ma50", False))
    if not (use_v or use_red or use_ma):
        return True, ""

    outcomes: list[tuple[str, bool | None]] = []
    if use_v:
        outcomes.append(("QQQ<VWAP", _qqq_below_session_vwap(broker, symbol="QQQ", now_et=now_et)))
    if use_red:
        outcomes.append(("QQQ red day", _qqq_red_day(broker, symbol="QQQ", last_price=qqq_last)))
    if use_ma:
        outcomes.append(("MA20<MA50", _qqq_ma20_below_ma50(broker, "QQQ")))

    any_true = any(x is True for _, x in outcomes)
    if any_true:
        passed = [k for k, v in outcomes if v is True]
        return True, "|".join(passed)

    all_none = all(v is None for _, v in outcomes)
    if all_none:
        return False, "score 2 SQQQ extra filters enabled but all data unavailable (VWAP/red/MA)"
    detail = ", ".join("%s=%s" % (k, v) for k, v in outcomes)
    return False, "score 2 SQQQ needs 1+ of enabled filters (%s)" % detail
