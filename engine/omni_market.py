"""Omni-market paper-research primitives.

This module is intentionally broker-free. It provides deterministic synthetic
research across FX/metals, futures, crypto/memecoins and options. Outputs are
probabilistic research diagnostics only; they are not forecasts of guaranteed
market outcomes and cannot place orders.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import erf, exp, log, pi, sqrt
from typing import Any

import numpy as np

PAPER_ONLY = True
LIVE_EXECUTION = False

MARKET_UNIVERSES: dict[str, tuple[str, ...]] = {
    "forex": ("EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "XAUUSD", "XAGUSD"),
    "futures": ("ES", "NQ", "GC", "CL", "BTC-PERP", "ETH-PERP"),
    "crypto": ("BTCUSDT", "ETHUSDT", "SOLUSDT"),
    "memecoin": ("DOGEUSDT", "SHIBUSDT", "BONKUSDT", "WIFUSDT", "PEPEUSDT"),
    "options": ("SPY", "QQQ", "BTC", "ETH"),
}

BASE_PRICES: dict[str, float] = {
    "EURUSD": 1.09, "GBPUSD": 1.28, "USDJPY": 150.0, "AUDUSD": 0.66,
    "USDCAD": 1.36, "XAUUSD": 2500.0, "XAGUSD": 29.0,
    "ES": 5400.0, "NQ": 19000.0, "GC": 2500.0, "CL": 72.0,
    "BTC-PERP": 68000.0, "ETH-PERP": 3600.0,
    "BTCUSDT": 68000.0, "ETHUSDT": 3600.0, "SOLUSDT": 160.0,
    "DOGEUSDT": 0.16, "SHIBUSDT": 0.00002, "BONKUSDT": 0.00003,
    "WIFUSDT": 2.2, "PEPEUSDT": 0.000012,
    "SPY": 560.0, "QQQ": 480.0, "BTC": 68000.0, "ETH": 3600.0,
}

VOL_SCALES: dict[str, float] = {
    "forex": 0.0007,
    "futures": 0.004,
    "crypto": 0.008,
    "memecoin": 0.028,
    "options": 0.01,
}


@dataclass(frozen=True)
class ResearchSnapshot:
    asset_class: str
    symbol: str
    price: float
    probability_up: float
    confidence: float
    score: float
    regime: str
    momentum: float
    volatility: float
    trend_strength: float
    liquidity_quality: float
    risk_score: float
    horizon: str
    agents: tuple[dict[str, Any], ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "asset_class": self.asset_class,
            "symbol": self.symbol,
            "price": self.price,
            "probability_up": self.probability_up,
            "confidence": self.confidence,
            "score": self.score,
            "regime": self.regime,
            "momentum": self.momentum,
            "volatility": self.volatility,
            "trend_strength": self.trend_strength,
            "liquidity_quality": self.liquidity_quality,
            "risk_score": self.risk_score,
            "horizon": self.horizon,
            "agents": list(self.agents),
            "paper_only": True,
            "live_execution": False,
            "data_source": "deterministic_synthetic_research",
        }


def _seed(symbol: str, seed: int) -> int:
    return (sum((i + 1) * ord(ch) for i, ch in enumerate(symbol)) + seed * 7919) % (2**32 - 1)


def _series(asset_class: str, symbol: str, seed: int, points: int = 240) -> np.ndarray:
    if asset_class not in MARKET_UNIVERSES:
        raise ValueError(f"unsupported asset class: {asset_class}")
    base = BASE_PRICES.get(symbol, 100.0)
    rng = np.random.default_rng(_seed(symbol, seed))
    scale = VOL_SCALES[asset_class]
    phase = np.linspace(0, 6 * pi, points)
    drift = rng.normal(0.0, scale * 0.05)
    cyclical = np.sin(phase + rng.uniform(-pi, pi)) * scale * 0.15
    noise = rng.normal(drift, scale, points)
    returns = noise + cyclical
    return base * np.exp(np.cumsum(returns))


def _clip(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, float(v)))


def _sigmoid(v: float) -> float:
    return 1.0 / (1.0 + exp(-max(-12.0, min(12.0, v))))


def _regime(trend_strength: float, volatility: float) -> str:
    if volatility > 0.02:
        return "high-volatility"
    if trend_strength > 0.68:
        return "trend"
    if trend_strength < 0.36:
        return "range"
    return "transition"


def research_snapshot(asset_class: str, symbol: str, seed: int = 7, horizon: str = "15m") -> ResearchSnapshot:
    asset_class = asset_class.lower()
    symbol = symbol.upper()
    prices = _series(asset_class, symbol, seed)
    rets = np.diff(np.log(prices))
    fast = float(np.mean(prices[-12:]))
    slow = float(np.mean(prices[-48:]))
    momentum = float(np.mean(rets[-12:]) / (np.std(rets[-48:]) + 1e-12))
    volatility = float(np.std(rets[-48:]))
    trend_strength = _clip(abs(fast / slow - 1.0) / max(volatility * 4.0, 1e-9))

    rng = np.random.default_rng(_seed(symbol + asset_class, seed + 19))
    liquidity_quality = _clip(rng.normal(0.72 if asset_class != "memecoin" else 0.48, 0.15))
    structural_risk = _clip(rng.normal(0.28 if asset_class in {"forex", "futures"} else 0.48, 0.16))
    if asset_class == "memecoin":
        structural_risk = _clip(structural_risk + 0.2)

    raw = momentum * 0.75 + (fast / slow - 1.0) / max(volatility, 1e-9) * 0.35
    probability_up = _clip(_sigmoid(raw), 0.05, 0.95)
    directional_edge = abs(probability_up - 0.5) * 2.0
    confidence = _clip(0.25 + 0.40 * directional_edge + 0.20 * trend_strength + 0.15 * liquidity_quality - 0.20 * structural_risk)
    score = round(100.0 * _clip(0.42 * confidence + 0.28 * trend_strength + 0.20 * liquidity_quality + 0.10 * directional_edge), 2)
    risk_score = round(100.0 * _clip(0.50 * structural_risk + 0.35 * min(volatility / max(VOL_SCALES[asset_class] * 2.0, 1e-9), 1.0) + 0.15 * (1.0 - liquidity_quality)), 2)
    regime = _regime(trend_strength, volatility)

    agent_inputs = {
        "Trend": trend_strength,
        "Momentum": _clip(0.5 + np.tanh(momentum) * 0.5),
        "Volatility": 1.0 - _clip(volatility / max(VOL_SCALES[asset_class] * 3.0, 1e-9)),
        "Liquidity": liquidity_quality,
        "Risk": 1.0 - risk_score / 100.0,
        "Critic": _clip(0.35 + confidence * 0.65),
    }
    agents = []
    for name, agent_score in agent_inputs.items():
        vote = "approve" if agent_score >= 0.62 else "neutral" if agent_score >= 0.42 else "reject"
        agents.append({"name": name, "vote": vote, "score": round(agent_score * 100.0, 1)})

    return ResearchSnapshot(
        asset_class=asset_class,
        symbol=symbol,
        price=round(float(prices[-1]), 8),
        probability_up=round(probability_up, 4),
        confidence=round(confidence, 4),
        score=score,
        regime=regime,
        momentum=round(momentum, 4),
        volatility=round(volatility, 6),
        trend_strength=round(trend_strength, 4),
        liquidity_quality=round(liquidity_quality, 4),
        risk_score=risk_score,
        horizon=horizon,
        agents=tuple(agents),
    )


def scan_markets(markets: list[str] | None = None, seed: int = 7, per_market: int = 7) -> dict[str, Any]:
    selected = [m.lower() for m in (markets or list(MARKET_UNIVERSES))]
    rows: list[dict[str, Any]] = []
    for market_index, market in enumerate(selected):
        universe = MARKET_UNIVERSES.get(market)
        if not universe:
            continue
        for symbol_index, symbol in enumerate(universe[:per_market]):
            row = research_snapshot(market, symbol, seed + market_index * 101 + symbol_index).as_dict()
            row["direction"] = "bullish" if row["probability_up"] >= 0.55 else "bearish" if row["probability_up"] <= 0.45 else "neutral"
            rows.append(row)
    rows.sort(key=lambda r: (float(r["score"]), float(r["confidence"])), reverse=True)
    return {
        "mode": "paper_research",
        "paper_only": True,
        "live_execution": False,
        "data_source": "deterministic_synthetic_research",
        "markets": selected,
        "rows": rows,
        "leader": rows[0] if rows else None,
    }


def forex_sniper(symbol: str = "XAUUSD", seed: int = 7) -> dict[str, Any]:
    snap = research_snapshot("forex", symbol, seed, horizon="5m/15m")
    direction = "LONG_RESEARCH" if snap.probability_up >= 0.56 else "SHORT_RESEARCH" if snap.probability_up <= 0.44 else "WAIT"
    components = {
        "htf_trend": round(snap.trend_strength * 100.0, 1),
        "momentum": round(_clip(0.5 + np.tanh(snap.momentum) * 0.5) * 100.0, 1),
        "liquidity": round(snap.liquidity_quality * 100.0, 1),
        "volatility_fit": round((1.0 - _clip(snap.volatility / 0.004)) * 100.0, 1),
        "risk_quality": round(100.0 - snap.risk_score, 1),
    }
    return {
        **snap.as_dict(),
        "research_direction": direction,
        "sniper_score": round(sum(components.values()) / len(components), 2),
        "components": components,
        "note": "High score means stronger synthetic confluence, not certainty or a trade recommendation.",
    }


def memecoin_radar(symbols: list[str] | None = None, seed: int = 7) -> dict[str, Any]:
    symbols = symbols or list(MARKET_UNIVERSES["memecoin"])
    rows: list[dict[str, Any]] = []
    for i, symbol in enumerate(symbols):
        snap = research_snapshot("memecoin", symbol.upper(), seed + i)
        rng = np.random.default_rng(_seed(symbol, seed + 500))
        holder_concentration = _clip(rng.normal(0.48, 0.18))
        contract_risk = _clip(rng.normal(0.35, 0.20))
        social_acceleration = _clip(rng.normal(0.55, 0.22))
        rug_risk = _clip(0.45 * holder_concentration + 0.35 * contract_risk + 0.20 * (1.0 - snap.liquidity_quality))
        quality = _clip(0.40 * snap.liquidity_quality + 0.25 * (1.0 - holder_concentration) + 0.20 * (1.0 - contract_risk) + 0.15 * snap.confidence)
        momentum_quality = _clip(0.55 * abs(snap.probability_up - 0.5) * 2.0 + 0.45 * social_acceleration)
        rows.append({
            **snap.as_dict(),
            "holder_concentration": round(holder_concentration, 4),
            "contract_risk": round(contract_risk, 4),
            "social_acceleration": round(social_acceleration, 4),
            "rug_risk": round(rug_risk * 100.0, 1),
            "quality_score": round(quality * 100.0, 1),
            "momentum_score": round(momentum_quality * 100.0, 1),
        })
    rows.sort(key=lambda r: (r["quality_score"], -r["rug_risk"]), reverse=True)
    return {
        "mode": "paper_research",
        "paper_only": True,
        "live_execution": False,
        "data_source": "deterministic_synthetic_research",
        "rows": rows,
        "note": "Risk flags are synthetic-model diagnostics and are not token due diligence.",
    }


def futures_lab(symbols: list[str] | None = None, seed: int = 7) -> dict[str, Any]:
    symbols = symbols or list(MARKET_UNIVERSES["futures"])
    rows = [research_snapshot("futures", symbol, seed + i, horizon="30m").as_dict() for i, symbol in enumerate(symbols)]
    for row in rows:
        row["basis_state"] = "normal" if row["risk_score"] < 60 else "stressed"
        row["session_bias"] = "trend" if row["trend_strength"] > 0.6 else "mean-reversion" if row["trend_strength"] < 0.35 else "mixed"
    rows.sort(key=lambda r: r["score"], reverse=True)
    return {"mode": "paper_research", "paper_only": True, "live_execution": False, "rows": rows, "data_source": "deterministic_synthetic_research"}


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + erf(x / sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return exp(-0.5 * x * x) / sqrt(2.0 * pi)


def options_lab(
    underlying: str,
    spot: float,
    strike: float,
    days: int,
    iv: float,
    rate: float = 0.04,
) -> dict[str, Any]:
    if spot <= 0 or strike <= 0 or days <= 0 or not 0.01 <= iv <= 3.0:
        raise ValueError("invalid theoretical option inputs")
    t = days / 365.0
    sigma_sqrt_t = iv * sqrt(t)
    d1 = (log(spot / strike) + (rate + 0.5 * iv * iv) * t) / sigma_sqrt_t
    d2 = d1 - sigma_sqrt_t
    disc = exp(-rate * t)
    call = spot * _norm_cdf(d1) - strike * disc * _norm_cdf(d2)
    put = strike * disc * _norm_cdf(-d2) - spot * _norm_cdf(-d1)
    gamma = _norm_pdf(d1) / (spot * sigma_sqrt_t)
    vega = spot * _norm_pdf(d1) * sqrt(t) / 100.0
    call_delta = _norm_cdf(d1)
    put_delta = call_delta - 1.0
    theta_call = (-(spot * _norm_pdf(d1) * iv) / (2.0 * sqrt(t)) - rate * strike * disc * _norm_cdf(d2)) / 365.0
    theta_put = (-(spot * _norm_pdf(d1) * iv) / (2.0 * sqrt(t)) + rate * strike * disc * _norm_cdf(-d2)) / 365.0
    expected_move = spot * iv * sqrt(t)
    prices = np.linspace(max(spot - expected_move * 2.0, spot * 0.25), spot + expected_move * 2.0, 41)
    payoff = []
    for px in prices:
        payoff.append({
            "underlying_price": round(float(px), 4),
            "long_call_pnl": round(max(float(px) - strike, 0.0) - call, 4),
            "long_put_pnl": round(max(strike - float(px), 0.0) - put, 4),
        })
    return {
        "mode": "theoretical_paper_research",
        "paper_only": True,
        "live_execution": False,
        "underlying": underlying.upper(),
        "inputs": {"spot": spot, "strike": strike, "days": days, "iv": iv, "rate": rate},
        "model": {
            "call_value": round(call, 4), "put_value": round(put, 4),
            "call_delta": round(call_delta, 4), "put_delta": round(put_delta, 4),
            "gamma": round(gamma, 6), "vega_per_vol_point": round(vega, 4),
            "call_theta_per_day": round(theta_call, 4), "put_theta_per_day": round(theta_put, 4),
            "expected_move": round(expected_move, 4),
        },
        "payoff": payoff,
        "note": "Black-Scholes-style educational scenario model using user-supplied inputs; no live options chain is queried.",
    }
