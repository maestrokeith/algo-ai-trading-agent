"""Autonomous command API for paper-only quantitative research.

The command center intentionally cannot place broker orders. It orchestrates
synthetic demo backtests, cross-instrument scans, bounded parameter sweeps,
risk diagnostics, and system-status queries only.
"""
from __future__ import annotations

from dataclasses import asdict
from math import isfinite
from typing import Any
from uuid import uuid4

from fastapi import APIRouter
from pydantic import BaseModel, Field, field_validator

from engine.paper_scalper import PaperScalperBacktester
from engine.trading_config import INSTRUMENTS, LIVE_EXECUTION, PAPER_ONLY, StrategyConfig
from src.api.routers.research import _build_config, _result_payload, _synthetic_frame

router = APIRouter(prefix="/api/command", tags=["paper-command-center"])

_BLOCKED_TERMS = {
    "live order",
    "real money",
    "real-money",
    "place order",
    "broker order",
    "withdraw",
    "deposit",
}

_SYMBOL_ALIASES = {
    "gold": "XAUUSD",
    "xau": "XAUUSD",
    "xauusd": "XAUUSD",
    "silver": "XAGUSD",
    "xag": "XAGUSD",
    "xagusd": "XAGUSD",
    "eurusd": "EURUSD",
    "euro dollar": "EURUSD",
    "gbpusd": "GBPUSD",
    "pound dollar": "GBPUSD",
    "usdjpy": "USDJPY",
    "dollar yen": "USDJPY",
    "audusd": "AUDUSD",
    "aussie dollar": "AUDUSD",
    "usdcad": "USDCAD",
    "dollar cad": "USDCAD",
}


class CommandRequest(BaseModel):
    command: str = Field(min_length=1, max_length=500)
    symbol: str = "XAUUSD"
    bars: int = Field(default=3500, ge=3500, le=10000)
    seed: int = Field(default=7, ge=0, le=1_000_000)

    @field_validator("symbol")
    @classmethod
    def validate_symbol(cls, value: str) -> str:
        key = value.upper().replace("/", "")
        if key not in INSTRUMENTS:
            raise ValueError(f"unsupported paper-research instrument: {value}")
        return key


class AutonomousCycleRequest(BaseModel):
    symbols: list[str] = Field(default_factory=lambda: ["XAUUSD", "XAGUSD", "EURUSD", "GBPUSD"], max_length=7)
    bars: int = Field(default=3500, ge=3500, le=6000)
    seed: int = Field(default=7, ge=0, le=1_000_000)

    @field_validator("symbols")
    @classmethod
    def validate_symbols(cls, values: list[str]) -> list[str]:
        clean: list[str] = []
        for value in values:
            key = value.upper().replace("/", "")
            if key not in INSTRUMENTS:
                raise ValueError(f"unsupported paper-research instrument: {value}")
            if key not in clean:
                clean.append(key)
        return clean


def _safe(value: Any) -> Any:
    if isinstance(value, float):
        return value if isfinite(value) else None
    return value


def _score(metrics: dict[str, Any]) -> float:
    """Research robustness score; never interpreted as a live-trading recommendation."""
    trades = float(metrics.get("trades") or 0)
    return_pct = float(metrics.get("return_pct") or 0)
    max_dd = float(metrics.get("max_drawdown") or 0)
    expectancy = float(metrics.get("expectancy") or 0)
    trade_penalty = 0.02 if trades < 5 else 0.0
    return round(return_pct - max_dd * 0.65 + expectancy * 0.0001 - trade_penalty, 6)


def _run_demo(symbol: str, bars: int, seed: int, cfg: StrategyConfig | None = None) -> dict[str, Any]:
    config = cfg or StrategyConfig()
    frame = _synthetic_frame(symbol, bars, seed)
    result = PaperScalperBacktester(config).run(symbol, frame, monte_carlo_simulations=25)
    payload = _result_payload(result, frame)
    payload["data_source"] = "deterministic_synthetic_demo"
    payload["research_score"] = _score(result.metrics)
    return payload


def _blocked(command: str) -> bool:
    text = command.lower()
    return any(term in text for term in _BLOCKED_TERMS)


def _symbols_from_text(command: str) -> list[str]:
    text = command.lower().replace("/", "")
    found: list[str] = []
    for alias, symbol in _SYMBOL_ALIASES.items():
        if alias in text and symbol not in found:
            found.append(symbol)
    return found


@router.get("/status")
async def command_status() -> dict[str, Any]:
    return {
        "status": "ready",
        "mode": "paper_research",
        "paper_only": PAPER_ONLY,
        "live_execution": LIVE_EXECUTION,
        "autonomy": "bounded_research_only",
        "capabilities": [
            "natural-language instrument selection",
            "system status",
            "single-instrument demo backtest",
            "cross-instrument research scan",
            "bounded parameter sweep",
            "risk diagnostics",
            "paper-research ranking",
        ],
    }


@router.post("/execute")
async def execute_command(request: CommandRequest) -> dict[str, Any]:
    mission_id = f"mission_{uuid4().hex[:12]}"
    text = request.command.strip().lower()
    mentioned = _symbols_from_text(text)
    target_symbol = mentioned[0] if mentioned else request.symbol

    if _blocked(text):
        return {
            "mission_id": mission_id,
            "intent": "blocked",
            "mode": "paper_research",
            "live_execution": False,
            "summary": "This command center is restricted to paper research and cannot perform real-money or broker-order actions.",
            "steps": ["Safety policy evaluated", "Live/broker action rejected", "Research boundary preserved"],
            "result": None,
        }

    if any(token in text for token in ("status", "health", "system")):
        return {
            "mission_id": mission_id,
            "intent": "status",
            "mode": "paper_research",
            "live_execution": False,
            "summary": "All command-center actions are constrained to paper research.",
            "steps": ["Check engine boundary", "Check supported instruments", "Return capability map"],
            "result": await command_status(),
        }

    if any(token in text for token in ("scan", "compare", "rank")):
        scan_symbols = mentioned or ["XAUUSD", "XAGUSD", "EURUSD", "GBPUSD"]
        cycle = await autonomous_cycle(
            AutonomousCycleRequest(symbols=scan_symbols, bars=request.bars, seed=request.seed)
        )
        return {
            "mission_id": mission_id,
            "intent": "cross_instrument_scan",
            "mode": "paper_research",
            "live_execution": False,
            "summary": f"Cross-instrument synthetic paper-research scan completed for {', '.join(scan_symbols)}.",
            "steps": ["Parse requested instruments", "Generate deterministic demo data", "Run paper backtests", "Score robustness", "Rank research outputs"],
            "result": cycle,
        }

    if any(token in text for token in ("optimize", "sweep", "tune")):
        candidates: list[dict[str, Any]] = []
        for stop_atr in (1.25, 1.5, 1.75):
            for rr in (1.0, 1.25, 1.5):
                cfg = _build_config({
                    "risk_fraction": 0.005,
                    "max_risk_fraction": 0.01,
                    "stop_atr_multiple": stop_atr,
                    "reward_risk": rr,
                })
                result = _run_demo(target_symbol, request.bars, request.seed, cfg)
                candidates.append({
                    "stop_atr_multiple": stop_atr,
                    "reward_risk": rr,
                    "score": result["research_score"],
                    "metrics": result["metrics"],
                })
        candidates.sort(key=lambda row: float(row["score"]), reverse=True)
        return {
            "mission_id": mission_id,
            "intent": "bounded_parameter_sweep",
            "mode": "paper_research",
            "live_execution": False,
            "summary": f"Completed a bounded 9-configuration paper sweep for {target_symbol}.",
            "steps": ["Parse requested instrument", "Lock risk at 0.5%", "Sweep ATR stop", "Sweep reward/risk", "Rank by return/drawdown/expectancy score"],
            "result": {"symbol": target_symbol, "candidates": candidates, "best": candidates[0]},
        }

    payload = _run_demo(target_symbol, request.bars, request.seed)
    return {
        "mission_id": mission_id,
        "intent": "demo_backtest",
        "mode": "paper_research",
        "live_execution": False,
        "summary": f"Completed deterministic paper research for {target_symbol}.",
        "steps": ["Parse requested instrument", "Generate deterministic demo market", "Compute multi-timeframe signals", "Apply strict risk engine", "Simulate paper fills", "Run Monte Carlo and walk-forward diagnostics"],
        "result": payload,
    }


@router.post("/autonomous-cycle")
async def autonomous_cycle(request: AutonomousCycleRequest) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for index, symbol in enumerate(request.symbols):
        payload = _run_demo(symbol, request.bars, request.seed + index)
        metrics = payload["metrics"]
        rows.append({
            "symbol": symbol,
            "score": payload["research_score"],
            "trades": int(metrics.get("trades") or 0),
            "win_rate": _safe(metrics.get("win_rate")),
            "return_pct": _safe(metrics.get("return_pct")),
            "max_drawdown": _safe(metrics.get("max_drawdown")),
            "profit_factor": _safe(metrics.get("profit_factor")),
            "expectancy": _safe(metrics.get("expectancy")),
        })
    rows.sort(key=lambda row: float(row["score"]), reverse=True)
    return {
        "mode": "paper_research",
        "paper_only": True,
        "live_execution": False,
        "cycle_id": f"cycle_{uuid4().hex[:12]}",
        "symbols": rows,
        "leader": rows[0] if rows else None,
        "config": asdict(StrategyConfig()) | {"instruments": "omitted"},
    }
