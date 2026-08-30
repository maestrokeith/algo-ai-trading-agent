#!/usr/bin/env python3
"""Poll Telegram for loop-control commands and launch/stop the trading loop."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.telegram_control import poll_and_dispatch


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    poll_and_dispatch(ROOT)


if __name__ == "__main__":
    main()
