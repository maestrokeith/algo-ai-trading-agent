"""
Algorithm layer (Lean-style): base class and context for event-driven backtest/live.

- QCAlgorithm: Initialize(context), OnEndOfDay(context, slice)
- AlgorithmContext: Time, Portfolio, order methods
- Slice: bar data for current time
"""

from .base import QCAlgorithm
from .context import AlgorithmContext, Portfolio, Slice, Bar, Position
from .trend_following import TrendFollowingAlgorithm

__all__ = ["QCAlgorithm", "AlgorithmContext", "Portfolio", "Slice", "Bar", "Position", "TrendFollowingAlgorithm"]
