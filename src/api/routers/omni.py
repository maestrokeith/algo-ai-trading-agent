"""Paper-only omni-market research API."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from engine.omni_market import (
    LIVE_EXECUTION,
    MARKET_UNIVERSES,
    PAPER_ONLY,
    forex_sniper,
    futures_lab,
    memecoin_radar,
    options_lab,
    research_snapshot,
    scan_markets,
)

router = APIRouter(prefix="/api/omni", tags=["omni-paper-research"])


class ScanRequest(BaseModel):
    markets: list[str] = Field(default_factory=lambda: ["forex", "futures", "crypto", "memecoin"], min_length=1, max_length=5)
    seed: int = Field(default=7, ge=0, le=1_000_000)


class ForecastRequest(BaseModel):
    asset_class: Literal["forex", "futures", "crypto", "memecoin"]
    symbol: str
    horizon: str = Field(default="15m", min_length=1, max_length=20)
    seed: int = Field(default=7, ge=0, le=1_000_000)


class SniperRequest(BaseModel):
    symbol: str = "XAUUSD"
    seed: int = Field(default=7, ge=0, le=1_000_000)


class MemeRequest(BaseModel):
    symbols: list[str] = Field(default_factory=lambda: list(MARKET_UNIVERSES["memecoin"]), min_length=1, max_length=12)
    seed: int = Field(default=7, ge=0, le=1_000_000)


class FuturesRequest(BaseModel):
    symbols: list[str] = Field(default_factory=lambda: list(MARKET_UNIVERSES["futures"]), min_length=1, max_length=12)
    seed: int = Field(default=7, ge=0, le=1_000_000)


class OptionsRequest(BaseModel):
    underlying: str = "SPY"
    spot: float = Field(default=560.0, gt=0)
    strike: float = Field(default=560.0, gt=0)
    days: int = Field(default=30, ge=1, le=730)
    iv: float = Field(default=0.22, ge=0.01, le=3.0)
    rate: float = Field(default=0.04, ge=-0.05, le=0.30)


def _validate_symbol(asset_class: str, symbol: str) -> str:
    key = symbol.upper()
    universe = MARKET_UNIVERSES.get(asset_class)
    if not universe or key not in universe:
        raise HTTPException(status_code=422, detail=f"unsupported {asset_class} research symbol: {symbol}")
    return key


@router.get("/status")
async def omni_status() -> dict:
    return {
        "status": "ready",
        "mode": "paper_research",
        "paper_only": PAPER_ONLY,
        "live_execution": LIVE_EXECUTION,
        "markets": {name: list(symbols) for name, symbols in MARKET_UNIVERSES.items()},
        "modules": [
            "forex sniper confluence",
            "cross-market probabilistic forecast",
            "memecoin synthetic risk radar",
            "futures regime lab",
            "theoretical options lab",
            "multi-agent research council",
            "omni-market ranking",
        ],
    }


@router.post("/scan")
async def omni_scan(request: ScanRequest) -> dict:
    invalid = [m for m in request.markets if m.lower() not in MARKET_UNIVERSES]
    if invalid:
        raise HTTPException(status_code=422, detail=f"unsupported markets: {', '.join(invalid)}")
    return scan_markets(request.markets, request.seed)


@router.post("/forecast")
async def forecast(request: ForecastRequest) -> dict:
    symbol = _validate_symbol(request.asset_class, request.symbol)
    payload = research_snapshot(request.asset_class, symbol, request.seed, request.horizon).as_dict()
    payload["direction"] = "bullish" if payload["probability_up"] >= 0.55 else "bearish" if payload["probability_up"] <= 0.45 else "neutral"
    payload["note"] = "Probability is a synthetic-model research output, not a guaranteed prediction."
    return payload


@router.post("/sniper")
async def sniper(request: SniperRequest) -> dict:
    symbol = _validate_symbol("forex", request.symbol)
    return forex_sniper(symbol, request.seed)


@router.post("/memecoin-radar")
async def meme(request: MemeRequest) -> dict:
    symbols = [_validate_symbol("memecoin", symbol) for symbol in request.symbols]
    return memecoin_radar(symbols, request.seed)


@router.post("/futures-lab")
async def futures(request: FuturesRequest) -> dict:
    symbols = [_validate_symbol("futures", symbol) for symbol in request.symbols]
    return futures_lab(symbols, request.seed)


@router.post("/options-lab")
async def options(request: OptionsRequest) -> dict:
    try:
        return options_lab(request.underlying, request.spot, request.strike, request.days, request.iv, request.rate)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
