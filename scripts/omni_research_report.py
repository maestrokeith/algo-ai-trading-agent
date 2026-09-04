#!/usr/bin/env python3
"""Generate a scheduled broker-free omni-market paper-research report."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from engine.omni_market import futures_lab, memecoin_radar, options_lab, scan_markets


def _load(path: str | None) -> dict:
    if not path:
        return {}
    try:
        return json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def build(previous: dict | None = None, seed: int | None = None) -> dict:
    now = datetime.now(timezone.utc)
    cycle_seed = int(seed if seed is not None else now.timestamp() // 1800) % 1_000_000
    scan = scan_markets(["forex", "futures", "crypto", "memecoin"], cycle_seed)
    memes = memecoin_radar(seed=cycle_seed + 100)
    futures = futures_lab(seed=cycle_seed + 200)
    options = options_lab("SPY", 560.0, 560.0, 30, 0.22)
    prior_history = list((previous or {}).get("history") or [])[-23:]
    leader = scan.get("leader")
    prior_history.append({
        "at": now.isoformat(),
        "leader": leader.get("symbol") if leader else None,
        "asset_class": leader.get("asset_class") if leader else None,
        "score": leader.get("score") if leader else None,
        "confidence": leader.get("confidence") if leader else None,
    })
    return {
        "generated_at": now.isoformat(),
        "mode": "paper_research",
        "paper_only": True,
        "live_execution": False,
        "data_source": "deterministic_synthetic_research",
        "seed": cycle_seed,
        "scan": scan,
        "memecoin_radar": memes,
        "futures_lab": futures,
        "options_reference": options,
        "history": prior_history,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--previous")
    parser.add_argument("--output", required=True)
    parser.add_argument("--seed", type=int)
    args = parser.parse_args()
    payload = build(_load(args.previous), args.seed)
    Path(args.output).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
