"""
One entry pass: longs → hedge → options. Call from the live loop on your cadence.

Example:

    report = entry_cycle(
        cfg=config,
        regime_score=regime_result.score,
        equity=account_equity,
        symbols=["QQQ", "NVDA"],
        get_bars=lambda s: broker.get_bars(s, timeframe="1Day", limit=120),
    )

Diagnostic pass (computes regime, logs sections, returns same report shape)::

    report = run_entry_cycle(
        cfg=config,
        equity=account_equity,
        symbols=symbols,
        get_bars=get_bars,
        compute_regime=lambda: regime_scorer.compute(regime_bars).score,
    )
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)

from .entry_signals import rsi_wilder_last, should_enter_long
from .hedge import (
    compute_hedge_size,
    hedge_allocation_pct,
    hedge_symbol,
    place_hedge_order,
)
from .options_alpha import options_signal_independent


@dataclass
class LongEvalResult:
    """Per-symbol ``should_enter_long`` (v2) using last close vs MA(*ma_period*) and RSI."""

    candidates: list[tuple[str, bool]] = field(default_factory=list)
    reasons: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class HedgeEvalResult:
    """Regime-based hedge target; ``hedge_pct`` is allocation before ``max_hedge_pct`` cap."""

    target_notional_usd: float = 0.0
    symbol: str = "SQQQ"
    hedge_pct: float = 0.0


@dataclass
class OptionsEvalResult:
    """``(symbol, ok, msg)`` from :func:`options_signal_independent`."""

    signals: list[tuple[str, bool, str]] = field(default_factory=list)


@dataclass
class EntryCycleReport:
    longs: LongEvalResult
    hedge: HedgeEvalResult
    options: OptionsEvalResult


def evaluate_longs(
    *,
    cfg: dict[str, Any],
    regime_score: int,
    symbols: Sequence[str],
    get_bars: Callable[[str], pd.DataFrame],
    ma_period: int | None = None,
    rsi_period: int | None = None,
) -> LongEvalResult:
    v2 = (cfg or {}).get("strategy_v2") or {}
    trend = (v2.get("signals") or {}).get("trend") or {}
    mom = (v2.get("signals") or {}).get("momentum") or {}
    ma_n = int(ma_period if ma_period is not None else trend.get("ma_slow", 50))
    rsi_n = int(rsi_period if rsi_period is not None else mom.get("rsi_period", 14))

    out = LongEvalResult()
    for sym in symbols:
        sym_u = str(sym).upper()
        df = get_bars(sym_u)
        if df is None or getattr(df, "empty", True) or "close" not in df.columns:
            _reason = f"{sym_u}: no bars"
            print(f"LONG skip — reason: {_reason}", flush=True)
            out.candidates.append((sym_u, False))
            out.reasons.append((sym_u, "no bars"))
            continue
        close_s = df["close"].astype(float)
        if len(close_s) < ma_n + 2:
            _reason = f"{sym_u}: short history"
            print(f"LONG skip — reason: {_reason}", flush=True)
            out.candidates.append((sym_u, False))
            out.reasons.append((sym_u, "short history"))
            continue
        price = float(close_s.iloc[-1])
        ma50 = float(close_s.rolling(ma_n).mean().iloc[-1])
        rsi = rsi_wilder_last(close_s, period=rsi_n)
        ok = should_enter_long(
            regime_score=regime_score,
            price=price,
            ma50=ma50,
            rsi=rsi,
            cfg=cfg,
        )
        out.candidates.append((sym_u, ok))
        out.reasons.append(
            (
                sym_u,
                "pass" if ok else "reject v2 long gate (trend/rsi/score)",
            )
        )
        if not ok:
            _reason = f"{sym_u}: reject v2 long gate (trend/rsi/score)"
            print(f"LONG skip — reason: {_reason}", flush=True)
    return out


def evaluate_hedge(
    *,
    cfg: dict[str, Any],
    regime_score: int,
    equity: float,
) -> HedgeEvalResult:
    sym = hedge_symbol(cfg)
    pct = hedge_allocation_pct(regime_score, cfg)
    usd = compute_hedge_size(regime_score, equity, cfg)
    return HedgeEvalResult(target_notional_usd=usd, symbol=sym, hedge_pct=pct)


def evaluate_hedge_place(
    regime_score: int,
    *,
    cfg: dict[str, Any],
    equity: float,
    broker: Any,
    execution_manager: Any,
    mid_price: float,
    spread_pct: float,
) -> tuple[HedgeEvalResult, Any | None, str | None]:
    """
    Evaluate hedge (pct + notional) then :func:`~src.strategy_v2.hedge.place_hedge_order`.

    Mirrors ``if score >= 2: 10% else: 25%`` then buy configured hedge symbol.
    """
    ev = evaluate_hedge(cfg=cfg, regime_score=regime_score, equity=equity)
    order, err = place_hedge_order(
        regime_score=regime_score,
        equity=equity,
        cfg=cfg,
        broker=broker,
        execution_manager=execution_manager,
        mid_price=mid_price,
        spread_pct=spread_pct,
    )
    return ev, order, err


def evaluate_options(
    *,
    cfg: dict[str, Any],
    symbols: Sequence[str],
    get_bars: Callable[[str], pd.DataFrame],
) -> OptionsEvalResult:
    rows: list[tuple[str, bool, str]] = []
    for sym in symbols:
        sym_u = str(sym).upper()
        df = get_bars(sym_u)
        ok, msg = options_signal_independent(sym_u, df=df, cfg=cfg)
        rows.append((sym_u, ok, msg))
    return OptionsEvalResult(signals=rows)


def entry_cycle(
    *,
    cfg: dict[str, Any],
    regime_score: int,
    equity: float,
    symbols: Sequence[str],
    get_bars: Callable[[str], pd.DataFrame],
    ma_period: int | None = None,
    rsi_period: int | None = None,
) -> EntryCycleReport:
    """Run ``evaluate_longs`` → ``evaluate_hedge`` → ``evaluate_options``."""
    longs = evaluate_longs(
        cfg=cfg,
        regime_score=regime_score,
        symbols=symbols,
        get_bars=get_bars,
        ma_period=ma_period,
        rsi_period=rsi_period,
    )
    hedge = evaluate_hedge(cfg=cfg, regime_score=regime_score, equity=equity)
    options = evaluate_options(cfg=cfg, symbols=symbols, get_bars=get_bars)
    return EntryCycleReport(longs=longs, hedge=hedge, options=options)


def _regime_score_from_compute(raw: Any) -> int:
    if hasattr(raw, "score"):
        return int(raw.score)
    return int(raw)


def run_entry_cycle(
    *,
    cfg: dict[str, Any],
    equity: float,
    symbols: Sequence[str],
    get_bars: Callable[[str], pd.DataFrame],
    compute_regime: Callable[[], Any],
    ma_period: int | None = None,
    rsi_period: int | None = None,
) -> EntryCycleReport:
    """
    ``regime_score = compute_regime()`` then longs → hedge → options (stdout progress lines
    plus INFO logs for per-symbol / hedge detail).

    ``compute_regime`` may return an ``int`` or any object with a ``score`` attribute
    (e.g. :class:`src.market_regime.RegimeResult`).
    """
    regime_score = _regime_score_from_compute(compute_regime())
    print("running longs...", flush=True)
    logger.info("run_entry_cycle longs regime_score=%s", regime_score)
    longs = evaluate_longs(
        cfg=cfg,
        regime_score=regime_score,
        symbols=symbols,
        get_bars=get_bars,
        ma_period=ma_period,
        rsi_period=rsi_period,
    )
    for (sym, ok), (_, reason) in zip(longs.candidates, longs.reasons, strict=True):
        logger.info("  %s %s — %s", sym, "Y" if ok else "N", reason)

    print("running hedge...", flush=True)
    hedge = evaluate_hedge(cfg=cfg, regime_score=regime_score, equity=equity)
    logger.info(
        "  %s hedge_pct=%.2f target_notional_usd=%.2f",
        hedge.symbol,
        hedge.hedge_pct,
        hedge.target_notional_usd,
    )

    print("running options...", flush=True)
    options = evaluate_options(cfg=cfg, symbols=symbols, get_bars=get_bars)
    for sym, ok, msg in options.signals:
        logger.info("  %s %s — %s", sym, "Y" if ok else "N", msg)

    return EntryCycleReport(longs=longs, hedge=hedge, options=options)
