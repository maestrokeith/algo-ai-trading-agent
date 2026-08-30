#!/usr/bin/env python3
"""Repo-root shim so ``python lean …`` keeps working; implementation is ``scripts/lean_cli.py``."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
_CLI = _ROOT / "scripts" / "lean_cli.py"
if not _CLI.is_file():
    sys.exit("algo: missing scripts/lean_cli.py")
raise SystemExit(
    subprocess.call([sys.executable, str(_CLI), *sys.argv[1:]], cwd=str(_ROOT))
)
