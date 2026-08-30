#!/usr/bin/env python3
"""Compatibility entry point for the combined daily summary CLI."""

from __future__ import annotations

from scripts.show_daily_summary import main


if __name__ == "__main__":
    raise SystemExit(main())
