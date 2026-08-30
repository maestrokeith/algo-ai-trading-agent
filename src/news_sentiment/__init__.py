"""NewsAPI headlines + FinBERT sentiment + rule engine (buy/sell signals)."""
from .rules import NewsRuleEngine, volume_spike_ratio, weak_trend_vs_ma
from .pipeline import NewsSentimentPipeline

__all__ = [
    "NewsRuleEngine",
    "volume_spike_ratio",
    "weak_trend_vs_ma",
    "NewsSentimentPipeline",
]
