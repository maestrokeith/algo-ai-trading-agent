#!/usr/bin/env python3

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.review_logs import ensure_paper_review_log


from src.app.live_loop import main


if __name__ == "__main__":
    if "--paper" in sys.argv or "--live" not in sys.argv:
        paper_log_path = ensure_paper_review_log(PROJECT_ROOT)
        print(f"PAPER_REVIEW_LOG path={paper_log_path} status=ready", flush=True)
    main()
