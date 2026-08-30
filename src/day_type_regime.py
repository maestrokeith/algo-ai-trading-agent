"""
Intraday **day type** classification: trend, chop, panic, squeeze, low-volatility drift.

Complements the daily :class:`src.market_regime.MarketRegimeScorer` (bullish / neutral / defensive)
with session structure from SPY 1-minute bars (and optional VIX context). Drives multipliers for
position size, entry/exit cadence, buy cooldowns, and deployable buying power (leverage book).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Literal


import pandas as pd

log = logging.getLogger(__name__)

DayTypeLabel = Literal[
    "panic_selloff",
    "trend_day",
    "squeeze_day",
    "chop_day",
    "low_volatility_drift",
    "unknown",
]


@dataclass(frozen=True)
class DayTypeResult:
    """One session label + tunable multipliers (1.0 = no change from base config)."""

    day_type: DayTypeLabel
    position_size_mult: float
    entry_interval_mult: float
    exit_interval_mult: float
    cooldown_mult: float
    gross_exposure_mult: float
    details: dict[str, float | int | str] = field(default_factory=dict)


def _f(cfg: dict[str, Any], key: str, default: float) -> float:
    try:
        return float(cfg.get(key, default))
    except (TypeError, ValueError):
        return float(default)


def _profile(
    profiles: dict[str, Any], name: str, key: str, default: float
) -> float:
    p = profiles.get(name) or {}
    if not isinstance(p, dict):
        return float(default)
    try:
        return float(p.get(key, default))
    except (TypeError, ValueError):
        return float(default)


def parse_day_type_profiles(
    config: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, float]]:
    """
    Return (thresholds_dict, empty) and read profiles from ``market_regime.day_types.profiles``.

    Thresholds are under ``market_regime.day_types``; each type's multipliers under ``profiles.<name>``.
    """
    cfg = config or {}
    dt = (cfg.get("market_regime") or {}).get("day_types")
    if not isinstance(dt, dict):
        return {}, {}
    th = {k: v for k, v in dt.items() if k != "profiles" and k != "enabled"}
    prof = dt.get("profiles")
    if not isinstance(prof, dict):
        prof = {}
    return th, prof


def _result_for(
    label: DayTypeLabel,
    profiles: dict[str, Any],
) -> DayTypeResult:
    return DayTypeResult(
        day_type=label,
        position_size_mult=_profile(profiles, label, "position_size_mult", 1.0),
        entry_interval_mult=_profile(profiles, label, "entry_interval_mult", 1.0),
        exit_interval_mult=_profile(profiles, label, "exit_interval_mult", 1.0),
        cooldown_mult=_profile(profiles, label, "cooldown_mult", 1.0),
        gross_exposure_mult=_profile(profiles, label, "gross_exposure_mult", 1.0),
        details={},
    )


def compute_day_type(
    spy_1m: pd.DataFrame | None,
    *,
    vix_last: float | None,
    vix_prev_close: float | None,
    config: dict[str, Any] | None = None,
) -> DayTypeResult:
    """
    Classify the current regular session from SPY 1m bars (session window pre-trimmed by caller).

    *entry_interval_mult* > 1.0 = **slower** entry scans (longer wall-clock gap).
    *exit_interval_mult* < 1.0 = **faster** exit passes.
    """
    th, profiles = parse_day_type_profiles(config)
    base_unknown = _result_for("unknown", profiles)

    if spy_1m is None or getattr(spy_1m, "empty", True) or len(spy_1m) < 12:
        return base_unknown

    need = {"open", "high", "low", "close"}
    if not need.issubset(set(spy_1m.columns)):
        return base_unknown

    o = spy_1m["open"].astype(float)
    h = spy_1m["high"].astype(float)
    l_ = spy_1m["low"].astype(float)
    c = spy_1m["close"].astype(float)
    n = len(c)
    session_open = float(o.iloc[0])
    last = float(c.iloc[-1])
    if session_open <= 0:
        return base_unknown

    hi = float(h.max())
    lo = float(l_.min())
    ret_sess = (last - session_open) / session_open * 100.0
    range_pct = (hi - lo) / session_open * 100.0
    green = int((c > o).sum())
    green_ratio = green / max(n, 1)

    # --- thresholds (tunable in YAML) ---
    panic_ret = _f(th, "panic_session_return_pct", -0.85)
    panic_vix_spike = _f(th, "panic_vix_change_ratio", 0.07)
    chop_range_max = _f(th, "chop_max_range_pct", 0.7)
    chop_abs_ret = _f(th, "chop_max_abs_return_pct", 0.12)
    trend_abs_min = _f(th, "trend_min_abs_return_pct", 0.35)
    trend_green_hi = _f(th, "trend_green_ratio_high", 0.57)
    trend_green_lo = _f(th, "trend_green_ratio_low", 0.43)
    drift_range_max = _f(th, "drift_max_range_pct", 1.0)
    drift_abs_min = _f(th, "drift_min_abs_return_pct", 0.08)
    drift_green_lo = _f(th, "drift_green_ratio_min", 0.48)
    drift_green_hi = _f(th, "drift_green_ratio_max", 0.56)
    squeeze_lookback = int(_f(th, "squeeze_narrow_lookback_bars", 30))
    squeeze_bw_max = _f(th, "squeeze_max_rel_range_pct", 0.45)
    squeeze_break = _f(th, "squeeze_breakout_min_range_expand_pct", 0.2)

    details: dict[str, float | int | str] = {
        "n_bars": n,
        "session_return_pct": round(ret_sess, 4),
        "range_pct": round(range_pct, 4),
        "green_ratio": round(green_ratio, 4),
    }

    vix_spike = False
    if (
        vix_last is not None
        and vix_prev_close is not None
        and float(vix_prev_close) > 0
    ):
        vr = (float(vix_last) - float(vix_prev_close)) / float(vix_prev_close)
        vix_spike = vr >= panic_vix_spike
        details["vix_change_ratio"] = round(vr, 4)

    # 1) Panic selloff
    if ret_sess <= panic_ret or (ret_sess <= panic_ret * 0.55 and vix_spike):
        r = _result_for("panic_selloff", profiles)
        return DayTypeResult(
            day_type=r.day_type,
            position_size_mult=r.position_size_mult,
            entry_interval_mult=r.entry_interval_mult,
            exit_interval_mult=r.exit_interval_mult,
            cooldown_mult=r.cooldown_mult,
            gross_exposure_mult=r.gross_exposure_mult,
            details={**details, "rule": "panic"},
        )

    # 2) Trend day (directional, broad participation)
    if abs(ret_sess) >= trend_abs_min and (
        green_ratio >= trend_green_hi or green_ratio <= trend_green_lo
    ):
        r = _result_for("trend_day", profiles)
        return DayTypeResult(
            day_type=r.day_type,
            position_size_mult=r.position_size_mult,
            entry_interval_mult=r.entry_interval_mult,
            exit_interval_mult=r.exit_interval_mult,
            cooldown_mult=r.cooldown_mult,
            gross_exposure_mult=r.gross_exposure_mult,
            details={**details, "rule": "trend"},
        )

    # 3) Squeeze day: was narrow, now expanding
    lb = max(8, min(squeeze_lookback, n // 2))
    early = spy_1m.iloc[:lb]
    late = spy_1m.iloc[-lb:]
    if len(early) >= 5 and len(late) >= 5:
        er = (
            float(early["high"].max()) - float(early["low"].min())
        ) / session_open * 100.0
        lr = (
            float(late["high"].max()) - float(late["low"].min())
        ) / session_open * 100.0
        details["early_range_pct"] = round(er, 4)
        details["late_range_pct"] = round(lr, 4)
        if er <= squeeze_bw_max and lr >= er + squeeze_break:
            r = _result_for("squeeze_day", profiles)
            return DayTypeResult(
                day_type=r.day_type,
                position_size_mult=r.position_size_mult,
                entry_interval_mult=r.entry_interval_mult,
                exit_interval_mult=r.exit_interval_mult,
                cooldown_mult=r.cooldown_mult,
                gross_exposure_mult=r.gross_exposure_mult,
                details={**details, "rule": "squeeze"},
            )

    # 4) Chop: tight range, stuck near flat
    if range_pct <= chop_range_max and abs(ret_sess) <= chop_abs_ret:
        r = _result_for("chop_day", profiles)
        return DayTypeResult(
            day_type=r.day_type,
            position_size_mult=r.position_size_mult,
            entry_interval_mult=r.entry_interval_mult,
            exit_interval_mult=r.exit_interval_mult,
            cooldown_mult=r.cooldown_mult,
            gross_exposure_mult=r.gross_exposure_mult,
            details={**details, "rule": "chop"},
        )

    # 5) Low-volatility drift: modest range, slow grind
    if (
        range_pct <= drift_range_max
        and abs(ret_sess) >= drift_abs_min
        and drift_green_lo <= green_ratio <= drift_green_hi
    ):
        r = _result_for("low_volatility_drift", profiles)
        return DayTypeResult(
            day_type=r.day_type,
            position_size_mult=r.position_size_mult,
            entry_interval_mult=r.entry_interval_mult,
            exit_interval_mult=r.exit_interval_mult,
            cooldown_mult=r.cooldown_mult,
            gross_exposure_mult=r.gross_exposure_mult,
            details={**details, "rule": "drift"},
        )

    return DayTypeResult(
        day_type=base_unknown.day_type,
        position_size_mult=base_unknown.position_size_mult,
        entry_interval_mult=base_unknown.entry_interval_mult,
        exit_interval_mult=base_unknown.exit_interval_mult,
        cooldown_mult=base_unknown.cooldown_mult,
        gross_exposure_mult=base_unknown.gross_exposure_mult,
        details={**details, "rule": "unknown"},
    )


def fetch_vix_context(
    broker: Any,
    vix_symbol: str,
    *,
    limit: int = 5,
) -> tuple[float | None, float | None]:
    """Latest daily close and previous daily close for VIX proxy (e.g. VIXY)."""
    try:
        b = broker.get_bars(str(vix_symbol).upper(), timeframe="1Day", limit=limit)
        if b is None or getattr(b, "empty", True) or len(b) < 2:
            return None, None
        cl = b["close"].astype(float)
        return float(cl.iloc[-1]), float(cl.iloc[-2])
    except Exception:
        return None, None
