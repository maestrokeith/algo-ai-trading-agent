"""Live package namespace.

Keep package initialization lightweight so submodules like
``src.live.session_clock`` can be imported without triggering circular imports
through the split exit surfaces.
"""

from __future__ import annotations

__all__: list[str] = []
