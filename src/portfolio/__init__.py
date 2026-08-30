"""Portfolio package namespace.

Keep package import side effects light so split modules can import each other without
triggering circular imports during test collection or live-loop startup. Import the specific
submodule you need directly, e.g. ``src.portfolio.allocator`` or
``src.portfolio.replacement_preflight``.
"""
from __future__ import annotations

__all__: list[str] = []
