"""Paper-only FX/metals research API.

This router exposes the existing research engine to the web dashboard. It
accepts historical OHLCV data, runs deterministic paper backtests, previews
risk, and exposes walk-forward/Monte-Carlo diagnostics. It intentionally has
no broker-order endpoint and cannot enable live execution.
"""
from __future__ import annotations

from dataclasses import asdict, fields, replace
from math import isfinite
from typing import Any, Literal

import numpy as np
import pandas as pd
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from engine.analytics import walk_forward_splits
from engine.paper_scalper import PaperScalperBacktester
from engine.risk_engine import RiskEngine
from engine.trading_config import INSTRUMENTS, LIVE_EXECUTION, PAPER_ONLY, StrategyConfig

router = APIRouter(prefix="/api/research", tags=["paper-research"])

_CONFIG_FIELDS = {f.name for f in fields(StrategyConfig)} - {"instruments"}


class OhlcvBar(BaseModel):
    timestamp: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    spread: float | None = None

    @field_validator("open", "high", "low", "close")
    @classmethod
    def positive_price(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("prices must be positive")
        return value


class BacktestRequest(BaseModel):
    symbol: str = "XAUUSD"
    bars: list[OhlcvBar] = Field(default_factory=list, max_length=20_000)
    config: dict[str, Any] = Field(default_factory=dict)
    monte_carlo_simulations: int = Field(default=250, ge=10, le=2_000)


class DemoRequest(BaseModel):
    symbol: str = "XAUUSD"
    bars: int = Field(default=5_000, ge=3_500, le=20_000)
    seed: int = Field(default=7, ge=0, le=1_000_000)
    config: dict[str, Any] = Field(default_factory=dict)
    monte_carlo_simulations: int = Field(default=250, ge=10, le=2_000)


class RiskPreviewRequest(BaseModel):
    symbol: str = "XAUUSD"
    side: Literal[-1, 1] = 1
    entry: float = Field(gt=0)
    atr: float = Field(gt=0)
    recent_low: float = Field(gt=0)
    recent_high: float = Field(gt=0)
    equity: float = Field(default=10_000.0, gt=0)
    config: dict[str, Any] = Field(default_factory=dict)


def _build_config(overrides: dict[str, Any]) -> StrategyConfig:
    unknown = sorted(set(overrides) - _CONFIG_FIELDS)
    if unknown:
        raise HTTPException(400, f"unsupported config fields: {', '.join(unknown)}")

    cfg = StrategyConfig()
    try:
        cfg = replace(cfg, **overrides)
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, f"invalid research config: {exc}") from exc

    if cfg.risk_fraction > 0.01 or cfg.max_risk_fraction > 0.01:
        raise HTTPException(400, "paper risk is capped at 1% per trade")
    return cfg


def _symbol(symbol: str) -> str:
    key = symbol.upper().replace("/", "")
    if key not in INSTRUMENTS:
        raise HTTPException(400, f"unsupported research instrument: {symbol}")
    return key


def _frame_from_bars(bars: list[OhlcvBar], symbol: str) -> pd.DataFrame:
    if len(bars) < 3_500:
        raise HTTPException(400, "at least 3500 one-minute bars are required for MTF warm-up")
    records = [bar.model_dump() for bar in bars]
    frame = pd.DataFrame(records)
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce")
    if frame["timestamp"].isna().any():
        raise HTTPException(400, "one or more timestamps are invalid")
    frame = frame.set_index("timestamp").sort_index()
    if frame.index.has_duplicates:
        frame = frame[~frame.index.duplicated(keep="last")]
    if "spread" in frame:
        frame["spread"] = frame["spread"].fillna(INSTRUMENTS[symbol].default_spread)
    return frame


def _synthetic_frame(symbol: str, count: int, seed: int) -> pd.DataFrame:
    spec = INSTRUMENTS[symbol]
    base_prices = {
        "EURUSD": 1.10,
        "GBPUSD": 1.28,
        "USDJPY": 145.0,
        "AUDUSD": 0.66,
        "USDCAD": 1.35,
        "XAUUSD": 2400.0,
        "XAGUSD": 30.0,
    }
    base = base_prices[symbol]
    rng = np.random.default_rng(seed)
    index = pd.date_range("2026-01-05", periods=count, freq="min", tz="UTC")

    # Alternating drift regimes create deterministic trend/chop samples without
    # pretending to be current market data.
    regime = np.where((np.arange(count) // 900) % 2 == 0, 1.0, -0.65)
    scale = max(spec.tick_size * 3.0, base * 0.00003)
    returns = rng.normal(0.0, scale, count) + regime * scale * 0.12
    close = np.maximum(base * 0.25, base + np.cumsum(returns))
    open_ = np.r_[close[0], close[:-1]]
    wick = np.abs(rng.normal(scale * 1.2, scale * 0.45, count))
    high = np.maximum(open_, close) + wick
    low = np.maximum(spec.tick_size, np.minimum(open_, close) - wick)
    volume = rng.integers(80, 220, count).astype(float)
    volume[(np.arange(count) % 37) == 0] *= 2.2
    spread = np.full(count, spec.default_spread, dtype=float)

    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume, "spread": spread},
        index=index,
    )


def _safe_number(value: Any) -> Any:
    if isinstance(value, (np.floating, float)):
        value = float(value)
        return value if isfinite(value) else None
    if isinstance(value, (np.integer, int)):
        return int(value)
    return value


def _records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for row in frame.to_dict(orient="records"):
        output.append({k: _safe_number(v) for k, v in row.items()})
    return output


def _result_payload(result, frame: pd.DataFrame) -> dict[str, Any]:
    mc = result.monte_carlo
    mc_summary = {
        "simulations": int(len(mc)),
        "median_ending_equity": _safe_number(mc["ending_equity"].median()) if len(mc) else None,
        "p05_ending_equity": _safe_number(mc["ending_equity"].quantile(0.05)) if len(mc) else None,
        "p95_max_drawdown": _safe_number(mc["max_drawdown"].quantile(0.95)) if len(mc) else None,
    }
    splits = walk_forward_splits(frame.index, folds=5, train_fraction=0.70)
    walk_forward = [
        {
            "fold": i + 1,
            "train_bars": len(train),
            "test_bars": len(test),
            "train_start": str(train[0]) if len(train) else None,
            "test_end": str(test[-1]) if len(test) else None,
        }
        for i, (train, test) in enumerate(splits)
    ]
    trades = []
    for trade in result.trades[-100:]:
        row = asdict(trade)
        row = {k: (str(v) if isinstance(v, pd.Timestamp) else _safe_number(v)) for k, v in row.items()}
        trades.append(row)
    return {
        "mode": "paper",
        "live_execution": False,
        "symbol": result.symbol,
        "bars": int(len(frame)),
        "metrics": {k: _safe_number(v) for k, v in result.metrics.items()},
        "instrument_stats": _records(result.instrument_stats),
        "session_stats": _records(result.session_stats),
        "monte_carlo": mc_summary,
        "walk_forward": walk_forward,
        "trades": trades,
    }


@router.get("/status")
async def research_status() -> dict[str, Any]:
    cfg = StrategyConfig()
    return {
        "status": "ready",
        "mode": "paper",
        "paper_only": PAPER_ONLY,
        "live_execution": LIVE_EXECUTION,
        "instruments": list(INSTRUMENTS),
        "modules": [
            "multi_timeframe_trend",
            "momentum_volume",
            "volatility_spread",
            "risk_position_sizing",
            "paper_execution",
            "dynamic_trade_management",
            "analytics_validation",
        ],
        "config": {k: v for k, v in asdict(cfg).items() if k != "instruments"},
    }


@router.post("/backtest")
async def backtest(request: BacktestRequest) -> dict[str, Any]:
    symbol = _symbol(request.symbol)
    cfg = _build_config(request.config)
    frame = _frame_from_bars(request.bars, symbol)
    try:
        result = PaperScalperBacktester(cfg).run(symbol, frame, request.monte_carlo_simulations)
    except (ValueError, KeyError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return _result_payload(result, frame)


@router.post("/demo-backtest")
async def demo_backtest(request: DemoRequest) -> dict[str, Any]:
    symbol = _symbol(request.symbol)
    cfg = _build_config(request.config)
    frame = _synthetic_frame(symbol, request.bars, request.seed)
    result = PaperScalperBacktester(cfg).run(symbol, frame, request.monte_carlo_simulations)
    payload = _result_payload(result, frame)
    payload["data_source"] = "deterministic_synthetic_demo"
    return payload


@router.post("/risk-preview")
async def risk_preview(request: RiskPreviewRequest) -> dict[str, Any]:
    symbol = _symbol(request.symbol)
    cfg = _build_config(request.config)
    try:
        plan = RiskEngine(cfg).plan(
            symbol=symbol,
            side=request.side,
            entry=request.entry,
            atr_value=request.atr,
            recent_low=request.recent_low,
            recent_high=request.recent_high,
            equity=request.equity,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {
        "mode": "paper",
        "live_execution": False,
        "plan": {k: _safe_number(v) for k, v in asdict(plan).items()},
    }
