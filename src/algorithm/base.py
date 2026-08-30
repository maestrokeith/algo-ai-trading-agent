"""
Lean-style algorithm base class (QCAlgorithm).

Override Initialize(context) and OnEndOfDay(context, slice).
Engine calls these in order; algorithm uses context to submit orders.
"""
from typing import Any

from .context import AlgorithmContext, Slice


class QCAlgorithm:
    """
    Base class for algorithms. Subclass and implement:
      - Initialize(context): set universe, indicators, etc.
      - OnEndOfDay(context, slice): called each bar/day with current data; place orders via context.
    """

    def __init__(self, config: dict[str, Any] | None = None):
        self.config = config or {}

    def initialize(self, context: AlgorithmContext) -> None:
        """Called once at start. Set universe, warmup, etc."""
        pass

    def on_end_of_day(self, context: AlgorithmContext, slice: Slice) -> None:
        """Called each trading day (or bar) with data for that time. Place orders via context."""
        pass

    def on_data(self, context: AlgorithmContext, data: Slice) -> None:
        """Alias for OnEndOfDay for Lean naming consistency."""
        self.on_end_of_day(context, data)
