"""Entry quality gates for live trend-long and dynamic candidates."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import pandas as pd

from src.dynamic_universe import session_vwap_from_bars
from src.exposure import SYMBOL_SECTOR
from src.signal_ranking import trend_long_composite_rank

SECTOR_CONFIRMATION_ETF: dict[str, str] = {
    "technology": "XLK",
    "tech": "XLK",
    "mega_cap": "QQQ",
    "mega-cap": "QQQ",
    "financials": "XLF",
    "energy": "XLE",
    "health": "XLV",
    "healthcare": "XLV",
    "consumer": "XLY",
    "discretionary": "XLY",
    "small_caps": "IWM",
    "broad_market": "SPY",
    "semis": "SMH",
    "semi": "SMH",
}

DYNAMIC_QUALITY_ROUTES = {
    "dynamic_universe",
    "dynamic_momentum",
    "dynamic_momentum_override",
    "momentum_breakout",
}


def aggressive_dynamic_mode_config(config: Mapping[str, Any] | None) -> Mapping[str, Any]:
    """Return the configured aggressive dynamic-entry profile, if present."""

    root = config if isinstance(config, Mapping) else {}
    dyn_entry = root.get("dynamic_entry") if isinstance(root.get("dynamic_entry"), Mapping) else {}
    aggr = dyn_entry.get("aggressive_mode") if isinstance(dyn_entry.get("aggressive_mode"), Mapping) else {}
    return aggr if isinstance(aggr, Mapping) else {}


def aggressive_dynamic_mode_enabled(config: Mapping[str, Any] | None) -> bool:
    aggr = aggressive_dynamic_mode_config(config)
    return _cfg_bool(aggr, "enabled", False)


@dataclass(frozen=True)
class EntryQualityDecision:
    allowed: bool
    reason: str
    quality_score: float
    sizing_multiplier: float
    starter: bool
    market_vwap_confirmed: bool
    symbol_vwap_confirmed: bool
    sector_confirmed: bool
    no_chase_ok: bool
    rejected_rules: tuple[str, ...] = ()
    features: Mapping[str, Any] | None = None
    entry_quality_score: float | None = None
    entry_quality_penalties: tuple[str, ...] = ()
    entry_quality_reason: str = ""
    positive_factors: tuple[str, ...] = ()
    negative_factors: tuple[str, ...] = ()
    adaptive_scoring_used: bool = False
    adaptive_entry: bool = False
    score_threshold: float | None = None
    size_multiplier_reason: str = ""
    score_components: Mapping[str, Mapping[str, float]] | None = None
    zero_score_factors: tuple[str, ...] = ()
    market_vwap_distance_pct: float | None = None
    market_vwap_slope: float | None = None
    market_vwap_score: float | None = None
    market_vwap_state: str = "unknown"
    market_vwap_data_available: bool = False


def _safe_float(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return out if math.isfinite(out) else None


def _last_close(df: pd.DataFrame | None) -> float | None:
    if df is None or getattr(df, "empty", True) or "close" not in df.columns:
        return None
    return _safe_float(df["close"].iloc[-1])


def _cfg_bool(cfg: Mapping[str, Any], key: str, default: bool) -> bool:
    if key not in cfg:
        return default
    raw = cfg.get(key)
    if isinstance(raw, str):
        return raw.strip().lower() not in {"0", "false", "no", "off"}
    return bool(raw)


def _cfg_float(cfg: Mapping[str, Any], key: str, default: float) -> float:
    out = _safe_float(cfg.get(key))
    return float(out) if out is not None else float(default)


def _cfg_int(cfg: Mapping[str, Any], key: str, default: int) -> int:
    out = _safe_float(cfg.get(key))
    return int(out) if out is not None else int(default)


def _entry_quality_weights(cfg: Mapping[str, Any]) -> Mapping[str, float]:
    raw = cfg.get("weights") if isinstance(cfg.get("weights"), Mapping) else {}
    defaults = {
        "trend": 20.0,
        "pullback": 15.0,
        "momentum": 20.0,
        "volume": 15.0,
        "symbol_vwap": 10.0,
        "market_vwap": 10.0,
        "regime": 5.0,
        "spread": 5.0,
    }
    return {key: _cfg_float(raw, key, value) for key, value in defaults.items()}


def _adaptive_scoring_configured(cfg: Mapping[str, Any]) -> bool:
    if "scoring_enabled" in cfg:
        return _cfg_bool(cfg, "scoring_enabled", False)
    return "threshold" in cfg or bool(cfg.get("adaptive_scoring", False))


def _regime_threshold(cfg: Mapping[str, Any], regime_score: int | None) -> float:
    raw = cfg.get("threshold")
    if isinstance(raw, Mapping):
        if regime_score is not None and int(regime_score) >= 4:
            return _cfg_float(raw, "strong_regime", _cfg_float(raw, "default", 80.0))
        if regime_score is not None and int(regime_score) <= 2:
            return _cfg_float(raw, "weak_regime", _cfg_float(raw, "default", 80.0))
        return _cfg_float(raw, "neutral_regime", _cfg_float(raw, "default", 80.0))
    if raw is not None:
        out = _safe_float(raw)
        if out is not None:
            return float(out)
    return 80.0


def _size_multiplier_for_score(cfg: Mapping[str, Any], score: float, threshold: float) -> tuple[float, str]:
    flex = cfg.get("flexible_entry") if isinstance(cfg.get("flexible_entry"), Mapping) else {}
    if not bool(flex.get("enabled", True)):
        return 1.0, "flexible_entry_disabled"
    raw = flex.get("size_by_score") if isinstance(flex.get("size_by_score"), Mapping) else {}
    bands: list[tuple[float, float]] = []
    for key, value in raw.items():
        k = _safe_float(key)
        v = _safe_float(value)
        if k is not None and v is not None:
            bands.append((float(k), max(0.0, min(1.0, float(v)))))
    if not bands:
        bands = [(90.0, 1.0), (85.0, 0.75), (80.0, 0.5), (75.0, 0.25)]
    for floor, mult in sorted(bands, key=lambda item: item[0], reverse=True):
        if score >= floor - 1e-9:
            return mult, "score_band_%.0f" % floor
    if score >= threshold - 1e-9:
        return min(mult for _, mult in bands), "threshold_floor"
    return 0.0, "score_below_threshold"


def _score_market_vwap(
    *,
    confirmed: bool,
    weight: float,
    distance_pct: float | None,
    slope: float | None,
    data_available: bool,
    cfg: Mapping[str, Any],
) -> tuple[float, str]:
    if not data_available:
        policy = str(cfg.get("unavailable_feature_policy", "conservative") or "conservative").strip().lower()
        if policy == "neutral":
            return weight * 0.5, "unavailable_neutral"
        return 0.0, "unavailable"
    if confirmed:
        return weight, "confirmed"
    recovering_distance = _cfg_float(cfg, "market_vwap_recovering_distance_pct", 0.25)
    if (
        distance_pct is not None
        and slope is not None
        and float(distance_pct) >= -abs(recovering_distance)
        and float(slope) > 0.0
    ):
        return weight * 0.5, "recovering"
    return 0.0, "deteriorating"


def _mapping_float(raw: Mapping[str, Any], key: str, default: float) -> float:
    out = _safe_float(raw.get(key))
    return float(out) if out is not None else float(default)


def aggressive_dynamic_fast_lane(
    *,
    cfg: Mapping[str, Any],
    news_score: float,
    catalyst_score: float,
    event_score: float,
    relative_volume: float | None,
    gain_pct: float | None,
) -> tuple[bool, str]:
    if _cfg_bool(cfg, "disable_fast_lane", False):
        return False, "disabled"
    fast = cfg.get("fast_lane") if isinstance(cfg.get("fast_lane"), Mapping) else {}
    if news_score >= _mapping_float(fast, "news_score", 3.0):
        return True, "news_score"
    if catalyst_score >= _mapping_float(fast, "catalyst_score", 0.25):
        return True, "catalyst_score"
    if event_score >= _mapping_float(fast, "event_score", 2.0):
        return True, "event_score"
    gain = _safe_float(gain_pct)
    rel = _safe_float(relative_volume)
    if gain is not None and gain >= _mapping_float(fast, "gap_or_gain_pct", 8.0):
        return True, "gap_or_gain"
    pairs = fast.get("rvol_gain_pairs") if isinstance(fast.get("rvol_gain_pairs"), list) else []
    for row in pairs:
        if not isinstance(row, Mapping):
            continue
        if (
            rel is not None
            and gain is not None
            and rel >= _mapping_float(row, "relative_volume", 999.0)
            and gain >= _mapping_float(row, "gain_pct", 999.0)
        ):
            return True, "relative_volume_gain"
    return False, "no_primary_trigger"


def aggressive_dynamic_price_tier(symbol_price: float | None) -> str:
    price = _safe_float(symbol_price)
    if price is None:
        return "unknown"
    if price < 2.0:
        return "sub_2"
    if price < 5.0:
        return "two_to_5"
    if price < 20.0:
        return "five_to_20"
    return "above_20"


def aggressive_dynamic_size_multiplier(cfg: Mapping[str, Any], *, score: float, price: float | None) -> tuple[float, str]:
    raw = cfg.get("size_by_score") if isinstance(cfg.get("size_by_score"), Mapping) else {}
    bands: list[tuple[float, float]] = []
    for key, value in raw.items():
        k = _safe_float(key)
        v = _safe_float(value)
        if k is not None and v is not None:
            bands.append((float(k), max(0.0, min(1.0, float(v)))))
    if not bands:
        bands = [(80.0, 1.0), (70.0, 0.65), (60.0, 0.40), (55.0, 0.25), (50.0, 0.15)]
    mult = 0.0
    score_band = "below_min"
    for floor, value in sorted(bands, key=lambda item: item[0], reverse=True):
        if float(score) >= floor - 1e-9:
            mult = value
            score_band = "score_band_%.0f" % floor
            break
    tier = aggressive_dynamic_price_tier(price)
    tier_cfg = cfg.get("price_tier_size") if isinstance(cfg.get("price_tier_size"), Mapping) else {}
    tier_mult = _mapping_float(
        tier_cfg,
        "two_to_5" if tier == "two_to_5" else ("five_to_20" if tier == "five_to_20" else "above_20"),
        0.35 if tier == "two_to_5" else (0.75 if tier == "five_to_20" else 1.0),
    )
    return min(mult, max(0.0, min(1.0, tier_mult))), "%s price_tier=%s" % (score_band, tier)


def aggressive_dynamic_cooldown_minutes(
    *,
    pnl_pct: float | None,
    material_catalyst: bool = False,
    config: Mapping[str, Any] | None = None,
) -> tuple[int, str]:
    cfg = aggressive_dynamic_mode_config(config)
    cd = cfg.get("cooldown_minutes") if isinstance(cfg.get("cooldown_minutes"), Mapping) else {}
    pnl = _safe_float(pnl_pct) or 0.0
    if material_catalyst and bool(cd.get("material_catalyst_reset", True)) and pnl >= 0:
        return 0, "material_catalyst_reset"
    if pnl > 0.10:
        return int(_mapping_float(cd, "profitable_exit", 10.0)), "profitable_exit"
    if pnl >= -0.10:
        return int(_mapping_float(cd, "scratch_exit", 15.0)), "scratch_exit"
    if pnl > -1.0:
        return int(_mapping_float(cd, "small_loss", 30.0)), "small_loss"
    return int(_mapping_float(cd, "large_loss", 60.0)), "large_loss"


def compute_aggressive_dynamic_entry_score(
    *,
    config: Mapping[str, Any] | None,
    price: float | None,
    news_score: float = 0.0,
    catalyst_score: float = 0.0,
    event_score: float = 0.0,
    relative_volume: float | None = None,
    gain_pct: float | None = None,
    trend_confirmed: bool | None = None,
    momentum_confirmed: bool | None = None,
    breakout_confirmed: bool | None = None,
    symbol_vwap_confirmed: bool | None = None,
    market_vwap_state: str | None = None,
    sector_confirmed: bool | None = None,
    regime_score: int | None = None,
) -> dict[str, Any]:
    cfg = aggressive_dynamic_mode_config(config)
    weights = cfg.get("weights") if isinstance(cfg.get("weights"), Mapping) else {}
    fast_lane, fast_trigger = aggressive_dynamic_fast_lane(
        cfg=cfg,
        news_score=float(news_score or 0.0),
        catalyst_score=float(catalyst_score or 0.0),
        event_score=float(event_score or 0.0),
        relative_volume=relative_volume,
        gain_pct=gain_pct,
    )
    components: dict[str, float] = {}
    zero: list[str] = []

    def add(name: str, earned: float, max_points: float) -> None:
        value = max(0.0, min(float(max_points), float(earned)))
        components[name] = round(value, 6)
        if value <= 1e-9:
            zero.append(name)

    catalyst_w = _mapping_float(weights, "catalyst", 25.0)
    catalyst_strength = max(float(news_score or 0.0) / 5.0, float(catalyst_score or 0.0) / 0.60, float(event_score or 0.0) / 3.0)
    add("catalyst", catalyst_w * min(1.0, catalyst_strength), catalyst_w)
    add("momentum", _mapping_float(weights, "momentum", 20.0) if momentum_confirmed else 0.0, _mapping_float(weights, "momentum", 20.0))
    rel = _safe_float(relative_volume)
    rel_w = _mapping_float(weights, "relative_volume", 15.0)
    add("relative_volume", rel_w * min(1.0, max(0.0, (rel or 0.0) / 2.0)), rel_w)
    gain = _safe_float(gain_pct)
    gain_w = _mapping_float(weights, "intraday_gain", 10.0)
    add("intraday_gain", gain_w * min(1.0, max(0.0, (gain or 0.0) / 8.0)), gain_w)
    add("trend", _mapping_float(weights, "trend", 10.0) * (1.0 if trend_confirmed else 0.5 if trend_confirmed is None else 0.0), _mapping_float(weights, "trend", 10.0))
    add("structure", _mapping_float(weights, "structure", 10.0) if breakout_confirmed else 0.0, _mapping_float(weights, "structure", 10.0))
    add("symbol_vwap", _mapping_float(weights, "symbol_vwap", 5.0) if symbol_vwap_confirmed else 0.0, _mapping_float(weights, "symbol_vwap", 5.0))
    market_w = _mapping_float(weights, "market_confirmation", 5.0)
    market_state = str(market_vwap_state or "unavailable").strip().lower()
    if market_state == "confirmed" and sector_confirmed is not False:
        market_points = market_w
    elif market_state in {"recovering", "mixed", "neutral"}:
        market_points = max(0.0, market_w - 2.5)
    elif market_state == "unavailable":
        market_points = max(0.0, market_w - 5.0)
    else:
        market_points = max(0.0, market_w - 5.0)
    add("market_confirmation", market_points, market_w)
    score = round(sum(components.values()), 6)
    threshold = _mapping_float(cfg, "normal_threshold", 60.0)
    if fast_lane:
        threshold = min(threshold, _mapping_float(cfg, "fast_lane_threshold", 50.0))
    elif regime_score is not None and int(regime_score) >= 4:
        threshold = min(threshold, _mapping_float(cfg, "strong_regime_threshold", 55.0))
    elif regime_score is not None and int(regime_score) <= 2:
        threshold = max(threshold, _mapping_float(cfg, "weak_regime_threshold", 65.0))
    severe_risk_off = bool(regime_score is not None and int(regime_score) <= 1)
    price_tier = aggressive_dynamic_price_tier(price)
    hard_reasons: list[str] = []
    if price_tier == "sub_2":
        hard_reasons.append("price_below_aggressive_minimum")
    if severe_risk_off and _cfg_bool(cfg, "block_severe_risk_off", True):
        hard_reasons.append("severe_risk_off")
    max_failures = int(_mapping_float(cfg, "max_noncritical_failures", 3.0))
    primary = bool(fast_lane or momentum_confirmed or (rel is not None and rel >= 2.0) or (gain is not None and gain >= 4.0) or breakout_confirmed)
    if not primary:
        hard_reasons.append("no_primary_trigger")
    if len(zero) > max_failures:
        hard_reasons.append("too_many_noncritical_failures")
    size, size_reason = aggressive_dynamic_size_multiplier(cfg, score=score, price=price)
    allowed = bool(not hard_reasons and score >= threshold - 1e-9)
    if fast_lane and not hard_reasons and score >= _mapping_float(cfg, "fast_lane_threshold", 50.0) - 1e-9:
        allowed = True
    return {
        "enabled": bool(cfg.get("enabled", False)),
        "allowed": allowed,
        "score": score,
        "threshold": threshold,
        "components": components,
        "zero_score_factors": tuple(zero),
        "noncritical_failures": len(zero),
        "fast_lane": fast_lane,
        "fast_lane_trigger": fast_trigger,
        "bypassed_noncritical_rules": tuple(zero[:max_failures]),
        "score_before_override": score,
        "score_after_override": score,
        "size_multiplier": size if allowed else 0.0,
        "size_reason": size_reason,
        "price_tier": price_tier,
        "hard_reasons": tuple(hard_reasons),
        "reason": "ok_aggressive_dynamic" if allowed else (hard_reasons[0] if hard_reasons else "aggressive_score_below_threshold"),
    }


def compute_weighted_entry_score(
    *,
    cfg: Mapping[str, Any],
    trend_confirmed: bool,
    pullback_confirmed: bool,
    momentum_confirmed: bool,
    volume_confirmed: bool,
    symbol_vwap_confirmed: bool,
    market_vwap_confirmed: bool,
    market_vwap_distance_pct: float | None = None,
    market_vwap_slope: float | None = None,
    market_vwap_data_available: bool = True,
    regime_confirmed: bool = True,
    news_score: float,
    catalyst_score: float,
    event_score: float,
    spread_tight: bool,
    relative_volume: float | None = None,
) -> tuple[
    float,
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
    bool,
    Mapping[str, Mapping[str, float]],
    tuple[str, ...],
    float,
    str,
]:
    weights = _entry_quality_weights(cfg)
    positive: list[str] = []
    negative: list[str] = []
    penalties: list[str] = []
    components: dict[str, dict[str, float]] = {}
    zero_factors: list[str] = []
    score = 0.0

    def add(name: str, ok: bool, points: float | None = None) -> None:
        nonlocal score
        max_points = float(weights[name])
        earned = max_points if ok else 0.0
        if points is not None:
            earned = max(0.0, min(max_points, float(points)))
        if ok:
            positive.append(name)
        else:
            negative.append(name)
        if earned <= 1e-9:
            zero_factors.append(name)
        score += earned
        components[name] = {"score": round(earned, 6), "max": round(max_points, 6)}

    add("trend", trend_confirmed)
    add("pullback", pullback_confirmed)
    add("momentum", momentum_confirmed)
    add("volume", volume_confirmed)
    add("symbol_vwap", symbol_vwap_confirmed)
    market_score, market_state = _score_market_vwap(
        confirmed=market_vwap_confirmed,
        weight=float(weights["market_vwap"]),
        distance_pct=market_vwap_distance_pct,
        slope=market_vwap_slope,
        data_available=bool(market_vwap_data_available),
        cfg=cfg,
    )
    add("market_vwap", market_score > 0.0, market_score)
    add("regime", regime_confirmed)
    add("spread", spread_tight)

    adaptive_market_vwap = bool(not market_vwap_confirmed and market_score > 0.0)
    if adaptive_market_vwap:
        penalty = max(0.0, float(weights["market_vwap"]) - market_score)
        penalties.append(f"market_vwap_penalty=-{penalty:g}")
        if "market_vwap" in negative:
            negative.remove("market_vwap")
        negative.append("market_vwap_penalized")
    return (
        round(score, 6),
        tuple(positive),
        tuple(negative),
        tuple(penalties),
        adaptive_market_vwap,
        components,
        tuple(zero_factors),
        round(market_score, 6),
        market_state,
    )


def price_above_session_vwap(df_1m: pd.DataFrame | None, *, price: float | None = None) -> bool:
    px = _safe_float(price) or _last_close(df_1m)
    vwap = session_vwap_from_bars(df_1m) if df_1m is not None else None
    return bool(px is not None and vwap is not None and float(vwap) > 0.0 and px >= float(vwap) - 1e-9)


def vwap_distance_pct(price: Any, vwap: Any) -> float | None:
    px = _safe_float(price)
    vw = _safe_float(vwap)
    if px is None or vw is None or vw <= 0.0:
        return None
    return (px - vw) / vw * 100.0


def atr_extension_pct(price: Any, vwap: Any, atr: Any) -> float | None:
    px = _safe_float(price)
    vw = _safe_float(vwap)
    atr_v = _safe_float(atr)
    if px is None or vw is None or atr_v is None or atr_v <= 0.0:
        return None
    return (px - vw) / atr_v * 100.0


def no_chase_ok(
    *,
    price: Any,
    vwap: Any,
    atr: Any = None,
    max_vwap_distance_pct: float = 1.5,
    max_atr_extension_pct: float = 200.0,
) -> tuple[bool, str]:
    dist = vwap_distance_pct(price, vwap)
    if dist is not None and dist > float(max_vwap_distance_pct) + 1e-9:
        return False, "vwap_distance_chase"
    ext = atr_extension_pct(price, vwap, atr)
    if ext is not None and ext > float(max_atr_extension_pct) + 1e-9:
        return False, "atr_extension_chase"
    return True, "ok"


def sector_confirmation_symbol(symbol: str, symbol_sector: Mapping[str, str] | None = None) -> str | None:
    sym = str(symbol or "").strip().upper()
    sectors = symbol_sector or SYMBOL_SECTOR
    sector = str(sectors.get(sym) or "").strip().lower()
    return SECTOR_CONFIRMATION_ETF.get(sector)


def strategy_quality_points(
    *,
    symbol_above_vwap: bool,
    spy_above_vwap: bool,
    qqq_above_vwap: bool,
    trend_5m_positive: bool,
    trend_15m_positive: bool,
    sector_confirmed: bool,
    relative_volume_ok: bool,
    spread_tight: bool,
    regime_score: int | None,
    extended_above_vwap: bool,
    wide_spread: bool,
) -> int:
    """Return the integer strategy quality score used by live entry gates."""

    score = 0
    score += 2 if symbol_above_vwap else 0
    score += 2 if spy_above_vwap else 0
    score += 2 if qqq_above_vwap else 0
    score += 2 if trend_5m_positive else 0
    score += 1 if trend_15m_positive else 0
    score += 1 if sector_confirmed else 0
    score += 1 if relative_volume_ok else 0
    score += 1 if spread_tight else 0
    if regime_score is not None and int(regime_score) <= 2:
        score -= 2
    if extended_above_vwap:
        score -= 2
    if wide_spread:
        score -= 2
    if not sector_confirmed:
        score -= 2
    return score


def trend_long_quality_score(
    df: pd.DataFrame,
    *,
    atr_pct: float | None,
    max_atr_pct: float,
    market_vwap_confirmed: bool,
    symbol_vwap_confirmed: bool,
    sector_confirmed: bool,
    no_chase_passed: bool,
    composite_weights: Mapping[str, float] | None = None,
) -> float:
    """Compatibility normalized score for existing research callers."""

    composite, _breakdown, denom = trend_long_composite_rank(
        df,
        atr_pct=atr_pct,
        max_atr_pct=max_atr_pct,
        composite_weights=composite_weights,
    )
    base = float(composite) / float(denom or 4.0)
    confirmations = (
        (0.15 if market_vwap_confirmed else -0.20)
        + (0.15 if symbol_vwap_confirmed else -0.25)
        + (0.10 if sector_confirmed else -0.15)
        + (0.10 if no_chase_passed else -0.25)
    )
    return max(0.0, min(1.0, base + confirmations))


def relative_strength_rank(candidates: list[Mapping[str, Any]], *, top_n: int) -> list[Mapping[str, Any]]:
    """Rank accepted candidates by relative strength inputs and return top N rows."""

    def flag(value: Any) -> float:
        return 1.0 if bool(value) else 0.0

    def num(value: Any) -> float:
        out = _safe_float(value)
        return float(out) if out is not None else 0.0

    def score(row: Mapping[str, Any]) -> float:
        return (
            num(row.get("day_gain_pct", row.get("gain_pct")))
            + num(row.get("relative_volume", row.get("rel_volume"))) * 2.0
            + flag(row.get("symbol_above_vwap", row.get("vwap_above"))) * 2.0
            + flag(row.get("alignment_1m", row.get("one_min_alignment"))) * 1.5
            + flag(row.get("alignment_5m", row.get("five_min_alignment"))) * 2.0
            + num(row.get("catalyst_score"))
            + flag(row.get("sector_confirmed"))
        )

    limit = max(0, int(top_n or 0))
    ranked = sorted(candidates, key=score, reverse=True)
    return [
        dict(row, relative_strength_score=score(row), relative_strength_rank=index + 1)
        for index, row in enumerate(ranked[:limit])
    ]


def adaptive_sleeve_size_multiplier(
    exits: list[Mapping[str, Any]],
    *,
    sleeve: str,
    weak_exit_threshold: int = 3,
) -> tuple[float, bool, int]:
    """Return size multiplier, block flag, and weak/loss count for a sleeve."""

    target = str(sleeve or "").strip().lower()
    count = 0
    for row in exits:
        route = str(row.get("sleeve") or row.get("entry_route") or row.get("route") or "").strip().lower()
        if route != target:
            continue
        pnl = _safe_float(row.get("pnl", row.get("realized_pnl")))
        pnl_pct = _safe_float(row.get("pnl_pct", row.get("realized_pnl_pct")))
        reason = str(row.get("exit_reason") or "").strip().lower()
        weak = (
            reason in {"weak_exit", "signal_flip", "stop_loss", "trailing_stop"}
            or (pnl is not None and pnl < 0.0)
            or (pnl_pct is not None and pnl_pct < 0.0)
        )
        if weak:
            count += 1
    threshold = max(1, int(weak_exit_threshold or 3))
    if count >= threshold:
        return 0.0, True, count
    if count >= 2:
        return 0.25, False, count
    if count >= 1:
        return 0.5, False, count
    return 1.0, False, count


def evaluate_entry_quality(
    *,
    route: str,
    symbol: str,
    df: pd.DataFrame,
    price: float,
    symbol_vwap: float | None,
    market_vwap_confirmed: bool,
    sector_confirmed: bool,
    regime_score: int | None,
    spy_above_vwap: bool | None = None,
    qqq_above_vwap: bool | None = None,
    trend_5m_positive: bool | None = None,
    trend_15m_positive: bool | None = None,
    relative_volume: float | None = None,
    spread_pct: float | None = None,
    is_live: bool = True,
    has_strong_catalyst: bool = False,
    atr: float | None = None,
    atr_pct: float | None = None,
    max_atr_pct: float = 8.0,
    config: Mapping[str, Any] | None = None,
    news_score: float | None = None,
    catalyst_score: float | None = None,
    event_score: float | None = None,
    momentum_confirmed: bool | None = None,
    pullback_confirmed: bool | None = None,
    volume_confirmed: bool | None = None,
    market_vwap_distance_pct: float | None = None,
    market_vwap_slope: float | None = None,
    market_vwap_data_available: bool | None = None,
    day_gain_pct: float | None = None,
    breakout_confirmed: bool | None = None,
) -> EntryQualityDecision:
    cfg = (config or {}).get("entry_quality") if isinstance(config, Mapping) else {}
    cfg = cfg if isinstance(cfg, Mapping) else {}
    if not _cfg_bool(cfg, "enabled", True):
        return EntryQualityDecision(True, "disabled", 999.0, 1.0, False, True, True, True, True)

    route_l = str(route or "").strip().lower()
    is_trend = route_l == "trend_long"
    is_dynamic_no_catalyst = route_l in DYNAMIC_QUALITY_ROUTES and not bool(has_strong_catalyst)
    if not (is_trend or is_dynamic_no_catalyst):
        return EntryQualityDecision(True, "route_not_quality_gated", 999.0, 1.0, False, True, True, True, True)

    symbol_vwap_confirmed = bool(symbol_vwap is not None and symbol_vwap > 0.0 and price >= symbol_vwap - 1e-9)
    spy_ok = bool(market_vwap_confirmed if spy_above_vwap is None else spy_above_vwap)
    qqq_ok = bool(market_vwap_confirmed if qqq_above_vwap is None else qqq_above_vwap)
    market_ok = bool(spy_ok and qqq_ok)
    market_data_available = bool(market_vwap_data_available if market_vwap_data_available is not None else True)
    no_chase_passed, chase_reason = no_chase_ok(
        price=price,
        vwap=symbol_vwap,
        atr=atr,
        max_vwap_distance_pct=_cfg_float(cfg, "max_vwap_extension_pct", _cfg_float(cfg, "max_vwap_distance_pct", 1.5)),
        max_atr_extension_pct=_cfg_float(cfg, "max_atr_extension", 2.0) * 100.0,
    )
    spread_tight = spread_pct is None or float(spread_pct) <= _cfg_float(cfg, "tight_spread_pct", 0.35)
    relative_volume_ok = bool(
        volume_confirmed if volume_confirmed is not None else (
            relative_volume is None or float(relative_volume) >= _cfg_float(cfg, "min_relative_volume", 0.75)
        )
    )
    trend_5m_ok = bool(trend_5m_positive if trend_5m_positive is not None else symbol_vwap_confirmed)
    trend_15m_ok = bool(trend_15m_positive if trend_15m_positive is not None else symbol_vwap_confirmed)
    trend_ok = bool(trend_5m_ok or trend_15m_ok)
    pullback_ok = bool(pullback_confirmed if pullback_confirmed is not None else True)
    regime_ok = bool(regime_score is None or int(regime_score) > _cfg_int(cfg, "weak_regime_score_lte", 2))
    score = float(
        strategy_quality_points(
            symbol_above_vwap=symbol_vwap_confirmed,
            spy_above_vwap=spy_ok,
            qqq_above_vwap=qqq_ok,
            trend_5m_positive=trend_5m_ok,
            trend_15m_positive=trend_15m_ok,
            sector_confirmed=bool(sector_confirmed),
            relative_volume_ok=bool(relative_volume_ok),
            spread_tight=bool(spread_tight),
            regime_score=regime_score,
            extended_above_vwap=not no_chase_passed,
            wide_spread=not spread_tight,
        )
    )
    features = {
        "symbol": str(symbol or "").upper(),
        "route": route_l,
        "symbol_above_vwap": symbol_vwap_confirmed,
        "spy_above_vwap": spy_ok,
        "qqq_above_vwap": qqq_ok,
        "market_vwap_confirmed": market_ok,
        "market_vwap_data_available": market_data_available,
        "market_vwap_distance_pct": market_vwap_distance_pct,
        "market_vwap_slope": market_vwap_slope,
        "sector_confirmed": bool(sector_confirmed),
        "relative_volume_ok": bool(relative_volume_ok),
        "spread_tight": bool(spread_tight),
        "vwap_distance_pct": vwap_distance_pct(price, symbol_vwap),
        "atr_extension_pct": atr_extension_pct(price, symbol_vwap, atr),
        "trend_5m_positive": trend_5m_ok,
        "trend_15m_positive": trend_15m_ok,
        "market_regime_score": regime_score,
    }
    news_score_f = _safe_float(news_score) or 0.0
    catalyst_score_f = _safe_float(catalyst_score) or 0.0
    event_score_f = _safe_float(event_score) or 0.0
    momentum_ok = bool(momentum_confirmed if momentum_confirmed is not None else trend_5m_ok)
    aggressive_dynamic = bool(route_l in DYNAMIC_QUALITY_ROUTES and aggressive_dynamic_mode_enabled(config))
    if aggressive_dynamic:
        market_state_for_aggressive = "confirmed" if market_ok else ("unavailable" if not market_data_available else "deteriorating")
        if not spread_tight:
            score_result = compute_aggressive_dynamic_entry_score(
                config=config,
                price=price,
                news_score=news_score_f,
                catalyst_score=catalyst_score_f,
                event_score=event_score_f,
                relative_volume=relative_volume,
                gain_pct=day_gain_pct,
                trend_confirmed=trend_ok,
                momentum_confirmed=momentum_ok,
                breakout_confirmed=breakout_confirmed,
                symbol_vwap_confirmed=symbol_vwap_confirmed,
                market_vwap_state=market_state_for_aggressive,
                sector_confirmed=sector_confirmed,
                regime_score=regime_score,
            )
            features = {
                **features,
                "aggressive_dynamic_mode": True,
                "aggressive_dynamic_score": score_result["score"],
                "aggressive_dynamic_threshold": score_result["threshold"],
                "aggressive_fast_lane": score_result["fast_lane"],
                "fast_lane_trigger": score_result["fast_lane_trigger"],
                "aggressive_dynamic_reason": "wide_spread",
            }
            return EntryQualityDecision(
                False,
                "wide_spread",
                float(score_result["score"]),
                0.0,
                False,
                market_ok,
                symbol_vwap_confirmed,
                bool(sector_confirmed),
                no_chase_passed,
                ("wide_spread",),
                features,
                entry_quality_score=float(score_result["score"]),
                entry_quality_reason="wide_spread",
                adaptive_scoring_used=True,
                adaptive_entry=False,
                score_threshold=float(score_result["threshold"]),
                zero_score_factors=tuple(score_result["zero_score_factors"]),
                market_vwap_distance_pct=market_vwap_distance_pct,
                market_vwap_slope=market_vwap_slope,
                market_vwap_state=market_state_for_aggressive,
                market_vwap_data_available=market_data_available,
            )
        score_result = compute_aggressive_dynamic_entry_score(
            config=config,
            price=price,
            news_score=news_score_f,
            catalyst_score=catalyst_score_f,
            event_score=event_score_f,
            relative_volume=relative_volume,
            gain_pct=day_gain_pct,
            trend_confirmed=trend_ok,
            momentum_confirmed=momentum_ok,
            breakout_confirmed=breakout_confirmed,
            symbol_vwap_confirmed=symbol_vwap_confirmed,
            market_vwap_state=market_state_for_aggressive,
            sector_confirmed=sector_confirmed,
            regime_score=regime_score,
        )
        features = {
            **features,
            "aggressive_dynamic_mode": True,
            "aggressive_dynamic_score": score_result["score"],
            "aggressive_dynamic_threshold": score_result["threshold"],
            "aggressive_fast_lane": score_result["fast_lane"],
            "fast_lane_trigger": score_result["fast_lane_trigger"],
            "bypassed_noncritical_rules": list(score_result["bypassed_noncritical_rules"]),
            "score_before_override": score_result["score_before_override"],
            "score_after_override": score_result["score_after_override"],
            "size_multiplier": score_result["size_multiplier"],
            "price_tier": score_result["price_tier"],
            "aggressive_dynamic_reason": score_result["reason"],
        }
        if not bool(score_result["allowed"]):
            return EntryQualityDecision(
                False,
                str(score_result["reason"]),
                float(score_result["score"]),
                0.0,
                False,
                market_ok,
                symbol_vwap_confirmed,
                bool(sector_confirmed),
                no_chase_passed,
                tuple(score_result["hard_reasons"]) or ("aggressive_score_below_threshold",),
                features,
                entry_quality_score=float(score_result["score"]),
                entry_quality_reason=str(score_result["reason"]),
                adaptive_scoring_used=True,
                adaptive_entry=False,
                score_threshold=float(score_result["threshold"]),
                zero_score_factors=tuple(score_result["zero_score_factors"]),
                market_vwap_distance_pct=market_vwap_distance_pct,
                market_vwap_slope=market_vwap_slope,
                market_vwap_state=market_state_for_aggressive,
                market_vwap_data_available=market_data_available,
            )
        return EntryQualityDecision(
            True,
            "aggressive_dynamic_fast_lane" if score_result["fast_lane"] else "aggressive_dynamic_score",
            float(score_result["score"]),
            float(score_result["size_multiplier"]),
            float(score_result["size_multiplier"]) < 1.0 - 1e-9,
            market_ok,
            symbol_vwap_confirmed,
            bool(sector_confirmed),
            no_chase_passed,
            (),
            features,
            entry_quality_score=float(score_result["score"]),
            entry_quality_reason=str(score_result["reason"]),
            adaptive_scoring_used=True,
            adaptive_entry=True,
            score_threshold=float(score_result["threshold"]),
            size_multiplier_reason=str(score_result["size_reason"]),
            zero_score_factors=tuple(score_result["zero_score_factors"]),
            market_vwap_distance_pct=market_vwap_distance_pct,
            market_vwap_slope=market_vwap_slope,
            market_vwap_state=market_state_for_aggressive,
            market_vwap_data_available=market_data_available,
        )
    adaptive_score = None
    positive_factors: tuple[str, ...] = ()
    negative_factors: tuple[str, ...] = ()
    penalties: tuple[str, ...] = ()
    adaptive_scoring_used = _adaptive_scoring_configured(cfg)
    adaptive_market_vwap = False
    score_components: Mapping[str, Mapping[str, float]] | None = None
    zero_score_factors: tuple[str, ...] = ()
    market_vwap_score: float | None = None
    market_vwap_state = "confirmed" if market_ok else "deteriorating"
    if adaptive_scoring_used:
        (
            adaptive_score,
            positive_factors,
            negative_factors,
            penalties,
            adaptive_market_vwap,
            score_components,
            zero_score_factors,
            market_vwap_score,
            market_vwap_state,
        ) = compute_weighted_entry_score(
            cfg=cfg,
            trend_confirmed=trend_ok,
            pullback_confirmed=pullback_ok,
            momentum_confirmed=momentum_ok,
            volume_confirmed=bool(relative_volume_ok),
            symbol_vwap_confirmed=bool(symbol_vwap_confirmed),
            market_vwap_confirmed=bool(market_ok),
            market_vwap_distance_pct=market_vwap_distance_pct,
            market_vwap_slope=market_vwap_slope,
            market_vwap_data_available=market_data_available,
            regime_confirmed=regime_ok,
            news_score=news_score_f,
            catalyst_score=catalyst_score_f,
            event_score=event_score_f,
            spread_tight=bool(spread_tight),
            relative_volume=relative_volume,
        )
        score = adaptive_score
        features = {
            **features,
            "entry_quality_score": adaptive_score,
            "entry_quality_penalties": list(penalties),
            "entry_quality_positive_factors": list(positive_factors),
            "entry_quality_negative_factors": list(negative_factors),
            "entry_quality_adaptive_market_vwap": adaptive_market_vwap,
            "entry_quality_components": score_components,
            "entry_quality_zero_score_factors": list(zero_score_factors),
            "market_vwap_score": market_vwap_score,
            "market_vwap_state": market_vwap_state,
            "news_score": news_score_f,
            "catalyst_score": catalyst_score_f,
            "event_score": event_score_f,
        }
    min_score = float(_cfg_int(cfg, "live_min_quality_score" if is_live else "paper_min_quality_score", 6 if is_live else 5))
    if adaptive_scoring_used:
        min_score = _regime_threshold(cfg, regime_score)
    if is_dynamic_no_catalyst:
        dyn_min_default = int(min_score) + 1 if not adaptive_scoring_used else int(min_score)
        min_score = max(min_score, float(_cfg_int(cfg, "dynamic_no_catalyst_min_quality_score", dyn_min_default)))
    weak_regime = is_trend and regime_score is not None and int(regime_score) <= _cfg_int(cfg, "weak_regime_score_lte", 2)
    weak_regime_min = float(_cfg_int(cfg, "weak_regime_live_min_quality_score", 8))

    rejected: list[str] = []
    if not adaptive_scoring_used and not market_ok:
        rejected.append("market_vwap_not_confirmed")
    if not adaptive_scoring_used and not symbol_vwap_confirmed:
        rejected.append("symbol_vwap_not_confirmed")
    if not sector_confirmed:
        rejected.append("sector_not_confirmed")
    if not no_chase_passed:
        rejected.append(chase_reason)
    if not spread_tight:
        rejected.append("wide_spread")
    if not adaptive_scoring_used and not relative_volume_ok:
        rejected.append("relative_volume_below_min")
    if not adaptive_scoring_used and weak_regime and is_live and score < weak_regime_min:
        rejected.append("regime_lte_2")
    if score < min_score - 1e-9:
        rejected.append("quality_score_below_min")
    if adaptive_scoring_used:
        noncritical_zero = tuple(
            factor
            for factor in zero_score_factors
            if factor in {"trend", "pullback", "momentum", "volume", "symbol_vwap", "market_vwap", "regime"}
        )
        max_zero = _cfg_int(cfg, "max_zero_noncritical_factors", 1)
        if len(noncritical_zero) > max_zero:
            rejected.append("too_many_zero_score_factors")
    hard_quality_rules = {
        "wide_spread",
        "vwap_distance_chase",
        "atr_extension_chase",
    }
    strong_override = bool(
        adaptive_scoring_used
        and _cfg_bool(cfg, "strong_news_override", True)
        and route_l in DYNAMIC_QUALITY_ROUTES
        and news_score_f >= 5.0
        and catalyst_score_f >= 0.60
        and event_score_f >= 3.0
    )
    override_used = False
    if strong_override and len(rejected) == 1 and rejected[0] not in hard_quality_rules:
        override_used = True
        penalties = tuple([*penalties, f"strong_catalyst_override={rejected[0]}"])
        rejected = []
    if rejected:
        features = {
            **features,
            "entry_quality_reason": rejected[0],
            "entry_quality_threshold": min_score,
            "entry_quality_penalties": list(penalties),
        }
        return EntryQualityDecision(
            False,
            rejected[0],
            score,
            0.0,
            False,
            market_ok,
            symbol_vwap_confirmed,
            bool(sector_confirmed),
            no_chase_passed,
            tuple(rejected),
            features,
            entry_quality_score=adaptive_score,
            entry_quality_penalties=penalties,
            entry_quality_reason=rejected[0],
            positive_factors=positive_factors,
            negative_factors=negative_factors,
            adaptive_scoring_used=adaptive_scoring_used,
            adaptive_entry=False,
            score_threshold=min_score,
            score_components=score_components,
            zero_score_factors=zero_score_factors,
            market_vwap_distance_pct=market_vwap_distance_pct,
            market_vwap_slope=market_vwap_slope,
            market_vwap_score=market_vwap_score,
            market_vwap_state=market_vwap_state,
            market_vwap_data_available=market_data_available,
        )

    starter_fraction = _cfg_float(cfg, "starter_size_fraction", 0.25)
    confirmation_score = _cfg_float(cfg, "confirmation_quality_score", 8.0)
    multiplier = 1.0
    starter = False
    size_reason = "full_quality"
    if adaptive_scoring_used:
        score_mult, size_reason = _size_multiplier_for_score(cfg, float(score), float(min_score))
        multiplier = min(multiplier, score_mult)
        starter = multiplier < 1.0 - 1e-9
    elif is_dynamic_no_catalyst or score < confirmation_score - 1e-9:
        multiplier = min(multiplier, starter_fraction)
        starter = True
        size_reason = "starter_size"
    if weak_regime and not is_live:
        multiplier = min(multiplier, _cfg_float(cfg, "weak_regime_paper_size_fraction", 0.5))
        starter = True
    reason = "starter_size" if starter else "confirmed"
    if adaptive_scoring_used:
        reason = "quality_score_passed"
    if not market_ok and adaptive_scoring_used:
        reason = "quality_score_passed_with_market_vwap_penalty"
    if override_used:
        reason = "strong_catalyst_override"
    features = {
        **features,
        "entry_quality_reason": reason,
        "entry_quality_penalties": list(penalties),
        "entry_quality_threshold": min_score,
        "entry_quality_size_multiplier": multiplier,
        "entry_quality_size_reason": size_reason,
    }
    return EntryQualityDecision(
        True,
        reason,
        score,
        multiplier,
        starter,
        market_ok,
        symbol_vwap_confirmed,
        bool(sector_confirmed),
        no_chase_passed,
        (),
        features,
        entry_quality_score=adaptive_score,
        entry_quality_penalties=penalties,
        entry_quality_reason=reason,
        positive_factors=positive_factors,
        negative_factors=negative_factors,
        adaptive_scoring_used=adaptive_scoring_used,
        adaptive_entry=bool(adaptive_scoring_used and (not market_ok or multiplier < 1.0 - 1e-9 or override_used)),
        score_threshold=min_score,
        size_multiplier_reason=size_reason,
        score_components=score_components,
        zero_score_factors=zero_score_factors,
        market_vwap_distance_pct=market_vwap_distance_pct,
        market_vwap_slope=market_vwap_slope,
        market_vwap_score=market_vwap_score,
        market_vwap_state=market_vwap_state,
        market_vwap_data_available=market_data_available,
    )
