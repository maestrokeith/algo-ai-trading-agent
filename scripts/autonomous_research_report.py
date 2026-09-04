"""Generate an autonomous, paper-only FX/metals research report.

This job is designed for GitHub Actions. It never connects to a broker and
never submits orders. It evaluates deterministic synthetic research markets,
ranks robustness metrics, maintains a small history, and emits JSON for the
AlgoSphere command website.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from math import isfinite
from pathlib import Path
from statistics import median
from typing import Any
from uuid import uuid4

from engine.paper_scalper import PaperScalperBacktester
from engine.trading_config import INSTRUMENTS, LIVE_EXECUTION, PAPER_ONLY, StrategyConfig
from src.api.routers.research import _synthetic_frame

DEFAULT_SYMBOLS = ("XAUUSD", "XAGUSD", "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD")


def _safe(value: Any) -> Any:
    if isinstance(value, float):
        return value if isfinite(value) else None
    return value


def _score(metrics: dict[str, Any]) -> float:
    trades = float(metrics.get("trades") or 0)
    return_pct = float(metrics.get("return_pct") or 0)
    max_dd = float(metrics.get("max_drawdown") or 0)
    expectancy = float(metrics.get("expectancy") or 0)
    low_sample_penalty = 0.02 if trades < 5 else 0.0
    return round(return_pct - max_dd * 0.65 + expectancy * 0.0001 - low_sample_penalty, 6)


def _read_previous(path: Path | None) -> dict[str, Any]:
    if path is None or not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _council(ranking: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not ranking:
        return [
            {
                "agent": "Safety Governor",
                "status": "PASS",
                "message": "Paper-only boundary intact; no live-order capability is present.",
            }
        ]

    leader = ranking[0]
    worst_dd = max(ranking, key=lambda row: float(row.get("max_drawdown") or 0.0))
    total_trades = sum(int(row.get("trades") or 0) for row in ranking)
    pf_values = [float(row["profit_factor"]) for row in ranking if row.get("profit_factor") is not None and isfinite(float(row["profit_factor"]))]
    median_pf = median(pf_values) if pf_values else 0.0
    average_score = sum(float(row["score"]) for row in ranking) / len(ranking)

    return [
        {
            "agent": "Market Scout",
            "status": "COMPLETE",
            "message": f"Evaluated {len(ranking)} deterministic FX/metals research environments.",
        },
        {
            "agent": "Strategy Analyst",
            "status": "COMPLETE",
            "message": f"Highest research robustness score in this synthetic cycle: {leader['symbol']} ({leader['score']:.4f}).",
        },
        {
            "agent": "Risk Sentinel",
            "status": "PASS" if float(worst_dd.get("max_drawdown") or 0.0) < 0.10 else "CAUTION",
            "message": f"Largest simulated drawdown observed: {float(worst_dd.get('max_drawdown') or 0.0) * 100:.2f}% on {worst_dd['symbol']}.",
        },
        {
            "agent": "Validation Agent",
            "status": "COMPLETE",
            "message": f"Cycle produced {total_trades} simulated trades; median profit factor {median_pf:.2f}.",
        },
        {
            "agent": "Research Synthesizer",
            "status": "NEUTRAL",
            "message": f"Average robustness score {average_score:.4f}. Results are research diagnostics, not a market forecast.",
        },
        {
            "agent": "Safety Governor",
            "status": "PASS",
            "message": "PAPER_ONLY=True and LIVE_EXECUTION=False verified before report generation.",
        },
    ]


def generate_report(
    *,
    symbols: tuple[str, ...] = DEFAULT_SYMBOLS,
    bars: int = 3500,
    seed: int = 7,
    previous: dict[str, Any] | None = None,
    history_limit: int = 48,
) -> dict[str, Any]:
    if PAPER_ONLY is not True or LIVE_EXECUTION is not False:
        raise RuntimeError("paper-only execution boundary is not intact")
    if bars < 3500:
        raise ValueError("bars must be >= 3500 for multi-timeframe warm-up")

    cfg = StrategyConfig()
    ranking: list[dict[str, Any]] = []

    for index, raw_symbol in enumerate(symbols):
        symbol = raw_symbol.upper().replace("/", "")
        if symbol not in INSTRUMENTS:
            raise ValueError(f"unsupported research instrument: {raw_symbol}")
        frame = _synthetic_frame(symbol, bars, seed + index)
        result = PaperScalperBacktester(cfg).run(symbol, frame, monte_carlo_simulations=20)
        metrics = result.metrics
        ranking.append(
            {
                "symbol": symbol,
                "score": _score(metrics),
                "trades": int(metrics.get("trades") or 0),
                "win_rate": _safe(metrics.get("win_rate")),
                "return_pct": _safe(metrics.get("return_pct")),
                "max_drawdown": _safe(metrics.get("max_drawdown")),
                "profit_factor": _safe(metrics.get("profit_factor")),
                "expectancy": _safe(metrics.get("expectancy")),
                "net_profit": _safe(metrics.get("net_profit")),
                "max_consecutive_losses": int(metrics.get("max_consecutive_losses") or 0),
            }
        )

    ranking.sort(key=lambda row: float(row["score"]), reverse=True)
    generated_at = datetime.now(timezone.utc).isoformat()
    leader = ranking[0] if ranking else None
    average_score = sum(float(row["score"]) for row in ranking) / len(ranking) if ranking else 0.0
    total_trades = sum(int(row["trades"]) for row in ranking)
    worst_dd = max((float(row.get("max_drawdown") or 0.0) for row in ranking), default=0.0)

    previous_history = (previous or {}).get("history", [])
    if not isinstance(previous_history, list):
        previous_history = []
    history = [
        *previous_history,
        {
            "generated_at": generated_at,
            "leader": leader["symbol"] if leader else None,
            "leader_score": leader["score"] if leader else None,
            "average_score": round(average_score, 6),
            "total_trades": total_trades,
            "worst_drawdown": worst_dd,
        },
    ][-history_limit:]

    return {
        "status": "ready",
        "mode": "paper_research",
        "paper_only": True,
        "live_execution": False,
        "data_source": "deterministic_synthetic_research",
        "generated_at": generated_at,
        "cycle_id": f"cloud_{uuid4().hex[:12]}",
        "bars_per_symbol": bars,
        "seed": seed,
        "ranking": ranking,
        "leader": leader,
        "summary": {
            "symbols_evaluated": len(ranking),
            "total_simulated_trades": total_trades,
            "average_score": round(average_score, 6),
            "worst_drawdown": worst_dd,
        },
        "history": history,
        "agent_council": _council(ranking),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="autonomy/latest.json")
    parser.add_argument("--previous", default="")
    parser.add_argument("--bars", type=int, default=3500)
    parser.add_argument("--seed", type=int, default=None)
    args = parser.parse_args()

    now = datetime.now(timezone.utc)
    seed = args.seed if args.seed is not None else int(now.strftime("%Y%m%d%H")) % 1_000_000
    previous_path = Path(args.previous) if args.previous else None
    report = generate_report(bars=args.bars, seed=seed, previous=_read_previous(previous_path))

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    print(f"wrote {output} cycle={report['cycle_id']} leader={report['leader']['symbol'] if report['leader'] else 'none'}")


if __name__ == "__main__":
    main()
