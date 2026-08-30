"""Legacy per-symbol sentiment facade.

Live trading now uses batch/cached catalyst scoring in ``src.news_catalyst``.
This facade intentionally does not call NewsAPI.
"""
from __future__ import annotations

import time
from typing import Any


class NewsSentimentPipeline:
    def __init__(self, config: dict[str, Any]):
        self.config = config
        ns = config.get("news_sentiment") or {}
        self.enabled = bool(ns.get("enabled", False))
        self.lookback_hours = int(ns.get("headline_lookback_hours", 24))
        self.max_headlines = int(ns.get("max_headlines", 15))
        self.cache_ttl_sec = float(ns.get("cache_ttl_seconds", 900))
        self.model_id = str(ns.get("finbert_model", "ProsusAI/finbert"))
        self._cache: dict[str, tuple[float, float]] = {}  # symbol -> (ts, score)

    def sentiment_for_symbol(self, symbol: str) -> float:
        """Disabled legacy per-symbol NewsAPI path; batch catalyst scoring is authoritative."""
        if not self.enabled:
            return 0.0
        key = symbol.upper()
        now = time.time()
        if key in self._cache:
            ts, sc = self._cache[key]
            if now - ts < self.cache_ttl_sec:
                return sc
        self._cache[key] = (now, 0.0)
        return 0.0
