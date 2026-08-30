#!/usr/bin/env python3
"""Compatibility wrapper for ``bin/algo autoops drill``."""
from __future__ import annotations

import sys

from scripts.run_autoops import main


if __name__ == "__main__":
    raise SystemExit(main(["drill", *sys.argv[1:]]))
