"""Rule engine: positive + volume spike → buy; negative + weak trend → sell."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping

import pandas as pd

_VALID_NEWS_OVERRIDE_MODES = frozenset({"off", "light", "full"})

_HIGH_CONVICTION_TYPE_ALIASES = {
    "earnings": "earnings_beat",
    "earnings_beat": "earnings_beat",
    "earnings beat": "earnings_beat",
    "analyst": "analyst_upgrade",
    "upgrade": "analyst_upgrade",
    "analyst_upgrade": "analyst_upgrade",
    "analyst upgrade": "analyst_upgrade",
    "acquisition": "acquisition",
    "m&a": "acquisition",
    "fda": "fda_approval",
    "fda_approval": "fda_approval",
    "fda approval": "fda_approval",
    "ai": "major_ai_partnership",
    "ai_partnership": "major_ai_partnership",
    "ai partnership": "major_ai_partnership",
    "major_ai_partnership": "major_ai_partnership",
    "partnership": "major_ai_partnership",
    "contract": "government_contract",
    "government_contract": "government_contract",
    "government contract": "government_contract",
}

_DEFAULT_HIGH_CONVICTION_THRESHOLDS = {
    "earnings_beat": {"min_news_score": 7.0, "min_event_score": 7.0, "min_catalyst_score": 8.0},
    "analyst_upgrade": {"min_news_score": 7.0, "min_event_score": 7.0, "min_catalyst_score": 8.0},
    "acquisition": {"min_news_score": 7.0, "min_event_score": 7.0, "min_catalyst_score": 8.0},
    "fda_approval": {"min_news_score": 7.0, "min_event_score": 7.0, "min_catalyst_score": 8.0},
    "major_ai_partnership": {"min_news_score": 7.0, "min_event_score": 7.0, "min_catalyst_score": 8.0},
    "government_contract": {"min_news_score": 7.0, "min_event_score": 7.0, "min_catalyst_score": 8.0},
}


def normalize_news_override_mode(raw: Any) -> str:
    """Map config ``news.override_mode`` to off | light | full (default full)."""
    m = str(raw or "full").strip().lower()
    return m if m in _VALID_NEWS_OVERRIDE_MODES else "full"


def _float_value(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return float(default)
    return out if math.isfinite(out) else float(default)


def _bool_value(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return bool(default)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "on", "y"}:
            return True
        if text in {"0", "false", "no", "off", "n", ""}:
            return False
    return bool(value)


def canonical_high_conviction_catalyst_type(catalyst_type: Any) -> str:
    """Normalize catalyst labels used by the controlled news override system."""
    raw = str(catalyst_type or "").strip().lower().replace("-", "_")
    return _HIGH_CONVICTION_TYPE_ALIASES.get(raw, raw or "unknown")


def high_conviction_news_override_config(config: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return merged high-conviction news override settings.

    The override may relax entry prefilters for explicitly configured catalyst classes. It is a
    signal-quality rule only; callers must continue running stop-loss, sizing, allocator, cash,
    exposure, and risk controls after this function returns ``allowed=True``.
    """
    cfg = config if isinstance(config, Mapping) else {}
    trading = cfg.get("trading") if isinstance(cfg.get("trading"), Mapping) else {}
    dyn = trading.get("dynamic") if isinstance(trading.get("dynamic"), Mapping) else {}
    raw = dyn.get("high_conviction_news_override") if isinstance(dyn, Mapping) else {}
    if not isinstance(raw, Mapping):
        du = cfg.get("dynamic_universe") if isinstance(cfg.get("dynamic_universe"), Mapping) else {}
        raw = du.get("high_conviction_news_override") if isinstance(du, Mapping) else {}
    if not isinstance(raw, Mapping):
        raw = {}

    global_thresholds = {
        "min_news_score": _float_value(raw.get("min_news_score"), 7.0),
        "min_event_score": _float_value(raw.get("min_event_score"), 7.0),
        "min_catalyst_score": _float_value(raw.get("min_catalyst_score"), 8.0),
        "min_relative_volume": _float_value(raw.get("min_relative_volume"), 1.5),
        "max_catalyst_age_minutes": _float_value(raw.get("max_catalyst_age_minutes"), 180.0),
    }
    thresholds: dict[str, dict[str, float]] = {}
    for ctype, defaults in _DEFAULT_HIGH_CONVICTION_THRESHOLDS.items():
        row = dict(defaults)
        row.update(global_thresholds)
        configured = raw.get("thresholds", {})
        if isinstance(configured, Mapping):
            override = configured.get(ctype)
            if isinstance(override, Mapping):
                for key in row:
                    if key in override:
                        row[key] = _float_value(override.get(key), row[key])
        thresholds[ctype] = row

    return {
        "enabled": _bool_value(raw.get("enabled"), default=False),
        "require_positive_sentiment": _bool_value(raw.get("require_positive_sentiment"), default=True),
        "global_thresholds": global_thresholds,
        "thresholds": thresholds,
    }


def evaluate_high_conviction_news_override(
    config: Mapping[str, Any] | None,
    *,
    catalyst_type: Any = None,
    news_score: Any = None,
    event_score: Any = None,
    catalyst_score: Any = None,
    relative_volume: Any = None,
    sentiment: Any = None,
    catalyst_age_minutes: Any = None,
) -> tuple[bool, str, float, dict[str, float]]:
    """Evaluate controlled high-conviction news override eligibility."""
    cfg = high_conviction_news_override_config(config)
    if not bool(cfg["enabled"]):
        return False, "override_disabled", 0.0, dict(cfg["global_thresholds"])

    normalized_type = canonical_high_conviction_catalyst_type(catalyst_type)
    thresholds = dict(cfg["thresholds"].get(normalized_type) or cfg["global_thresholds"])
    if normalized_type not in cfg["thresholds"]:
        return False, "unsupported_catalyst_type", 0.0, thresholds

    news = _float_value(news_score)
    event = _float_value(event_score)
    catalyst = _float_value(catalyst_score)
    catalyst_scaled = catalyst * 10.0 if 0.0 < catalyst <= 1.0 else catalyst
    score_eff = max(news, event, catalyst_scaled)
    score_ok = (
        news >= thresholds["min_news_score"]
        or event >= thresholds["min_event_score"]
        or catalyst_scaled >= thresholds["min_catalyst_score"]
    )
    if not score_ok:
        return False, "score_below_threshold", score_eff, thresholds

    if catalyst_age_minutes is not None:
        age = _float_value(catalyst_age_minutes, math.inf)
        if age > thresholds["max_catalyst_age_minutes"]:
            return False, "stale_catalyst", score_eff, thresholds

    rel_vol = _float_value(relative_volume)
    if rel_vol < thresholds["min_relative_volume"]:
        return False, "relative_volume_below_threshold", score_eff, thresholds

    if bool(cfg["require_positive_sentiment"]) and _float_value(sentiment) <= 0.0:
        return False, "non_positive_sentiment", score_eff, thresholds

    return True, f"high_conviction_{normalized_type}", score_eff, thresholds


def volume_spike_ratio(df: pd.DataFrame, lookback: int = 20) -> float | None:
    """Last bar volume / average of prior `lookback` days. None if not computable."""
    if df is None or df.empty or "volume" not in df.columns:
        return None
    n = len(df)
    if n < lookback + 1:
        return None
    vol = df["volume"].astype(float)
    last = float(vol.iloc[-1])
    prev = vol.iloc[-lookback - 1 : -1]
    avg = float(prev.mean())
    if avg <= 0:
        return None
    return last / avg


def weak_trend_vs_ma(df: pd.DataFrame, ma_period: int) -> bool:
    """True if close is below MA(ma_period) (weak / distribution)."""
    if df is None or df.empty or "close" not in df.columns:
        return False
    if len(df) < ma_period:
        return False
    close = float(df["close"].iloc[-1])
    ma = float(df["close"].rolling(ma_period).mean().iloc[-1])
    return close < ma


@dataclass
class NewsRuleEngine:
    """Configurable thresholds from config['news_sentiment']."""

    positive_score_threshold: float = 0.12
    negative_score_threshold: float = -0.12
    volume_spike_min: float = 1.5
    weak_trend_ma_period: int = 20

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "NewsRuleEngine":
        ns = config.get("news_sentiment") or {}
        return cls(
            positive_score_threshold=float(ns.get("positive_score_threshold", 0.12)),
            negative_score_threshold=float(ns.get("negative_score_threshold", -0.12)),
            volume_spike_min=float(ns.get("volume_spike_min", 1.5)),
            weak_trend_ma_period=int(ns.get("weak_trend_ma_period", 20)),
        )

    def should_buy(self, sentiment_score: float, vol_ratio: float | None) -> bool:
        if vol_ratio is None:
            return False
        return sentiment_score >= self.positive_score_threshold and vol_ratio >= self.volume_spike_min

    def should_sell(self, sentiment_score: float, df: pd.DataFrame) -> bool:
        if sentiment_score > self.negative_score_threshold:
            return False
        return weak_trend_vs_ma(df, self.weak_trend_ma_period)
